"""Offline native-GDAL/optional-Qt validation using new synthetic data only."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from generate_samples import generate

from geopublisher.config import Environment
from geopublisher.engine import run_plan
from geopublisher.plans import build_plan, digest_file, load_plan, verify_artifacts


def expect_code(operation, expected: str) -> None:
    try:
        operation()
    except Exception as exc:
        if getattr(exc, "code", None) != expected:
            raise AssertionError(f"Expected {expected}; received an unexpected failure.") from exc
    else:
        raise AssertionError(f"Expected {expected}; operation unexpectedly succeeded.")


def check_desktop(environment: Environment, inputs: Path, output: Path, font: Path | None) -> dict:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox

    from geopublisher.desktop import MainWindow

    app = QApplication.instance() or QApplication([])
    if font:
        font_id = QFontDatabase.addApplicationFont(str(font))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if not families:
            raise ValueError("The explicitly selected local font could not be loaded.")
        app.setFont(QFont(families[0], 10))
    window = MainWindow(work_dir=output / "桌面任务")
    window.apply_environment(environment)
    window.add_sources([str(inputs)])
    window.show()
    app.processEvents()
    warnings = []

    def capture_desktop(name: str) -> None:
        # Let native style transitions finish before recording visual evidence.
        QTest.qWait(300)
        snapshot = window.grab()
        if snapshot.isNull() or not snapshot.save(str(output / name), "PNG"):
            raise RuntimeError(f"Could not save desktop evidence: {name}")

    def wait_for_worker() -> None:
        deadline = time.monotonic() + 180
        while window._thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        if window._thread is not None:
            raise RuntimeError("Desktop native preflight did not finish within 180 seconds.")

    with patch.object(QMessageBox, "warning", side_effect=lambda *args: warnings.append(args[2])):
        try:
            assert not window.independent_tiffs.isChecked()
            QTest.mouseClick(window.inspect_button, Qt.MouseButton.LeftButton)
            wait_for_worker()
            assert window._plan is None and not window.execute_button.isEnabled()
            assert len(warnings) == 1 and "RASTER_INTENT_REQUIRED" in warnings[0]
            warnings.clear()
            QTest.mouseClick(
                window.independent_tiffs,
                Qt.MouseButton.LeftButton,
                pos=QPoint(8, window.independent_tiffs.height() // 2),
            )
            assert window.independent_tiffs.isChecked()
            capture_desktop("desktop-selection.png")
            assert window.independent_tiffs.isChecked()
            QTest.mouseClick(window.inspect_button, Qt.MouseButton.LeftButton)
            wait_for_worker()
            assert not warnings, warnings
            plan = window._plan
            assert plan and plan["ready"] and len(plan["resources"]) == 2
            assert plan["import_intent"]["raster_mode"] == "independent"
            assert window.execute_button.isEnabled()
            assert "2 个 TIFF 分别发布为 2 个独立图层" in window.plan_summary.text()
            capture_desktop("desktop-review.png")
            extra = output / "额外影像.tif"
            shutil.copyfile(inputs / "east.tif", extra)
            window.add_sources([str(extra)])
            assert not window.independent_tiffs.isChecked() and window._plan is None
            assert not window.execute_button.isEnabled()
        finally:
            if window._thread is not None:
                # Wait for the completion slot as well as the native thread;
                # closing early would open the desktop's busy-task dialog.
                window._thread.quit()
                while window._thread is not None:
                    app.processEvents()
                    time.sleep(0.01)
            window.close()
            app.processEvents()
    return {
        "platform": app.platformName(),
        "unchecked_blocked": True,
        "selection_checked": True,
        "background_ready": True,
        "resource_count": 2,
        "restored_mode": load_plan(Path(plan["output_dir"]) / "plan.json")["import_intent"]["raster_mode"],
        "selection_change_cleared_confirmation": True,
        "publication_clicked": False,
    }


def validate(gdal_bin: Path, output: Path, *, desktop: bool = False, font: Path | None = None) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    counts = {"native_subprocess_calls": 0, "network_attempts": 0, "client_creation_attempts": 0}
    original_run = subprocess.run

    def observed_run(*args, **kwargs):
        counts["native_subprocess_calls"] += 1
        return original_run(*args, **kwargs)

    def no_network(*args, **kwargs):
        counts["network_attempts"] += 1
        raise AssertionError("Offline validation attempted a network connection.")

    def no_client(*args, **kwargs):
        counts["client_creation_attempts"] += 1
        raise AssertionError("A blocked plan attempted to create an HTTP client.")

    with ExitStack() as stack:
        stack.enter_context(patch.object(subprocess, "run", side_effect=observed_run))
        stack.enter_context(patch.object(socket.socket, "connect", side_effect=no_network))
        stack.enter_context(patch.object(socket.socket, "connect_ex", side_effect=no_network))
        stack.enter_context(patch.object(Environment, "client", side_effect=no_client))
        samples = output / "合成数据"
        generate(gdal_bin.resolve(), samples)
        inputs = output / "中文及 空格" / "独立影像"
        inputs.mkdir(parents=True)
        for name in ("east.tif", "west.tif"):
            shutil.copyfile(samples / "rgb_gradient.tif", inputs / name)
        before = {path.name: digest_file(path) for path in inputs.iterdir()}
        environment = Environment(
            "synthetic-import-check", "http://127.0.0.1:18880/geoserver", "test", str(gdal_bin.resolve())
        )
        prior_calls = counts["native_subprocess_calls"]
        expect_code(
            lambda: build_plan(environment, [inputs], output / "未确认任务"), "RASTER_INTENT_REQUIRED"
        )
        assert counts["native_subprocess_calls"] == prior_calls
        assert not (output / "未确认任务").exists()
        plan = build_plan(environment, [inputs], output / "已确认任务", raster_mode="independent")
        assert plan["ready"] and len(plan["resources"]) == 2
        assert all(r["inspection"]["crs"] == "EPSG:4326" for r in plan["resources"])
        restored = load_plan(Path(plan["output_dir"]) / "plan.json")
        assert restored == plan
        verify_artifacts(restored)
        extra = inputs / "new.tif"
        shutil.copyfile(inputs / "east.tif", extra)
        expect_code(lambda: run_plan(restored), "IMPORT_SELECTION_CHANGED")
        extra.unlink()
        marker = inputs.parent / "pyramid.properties"
        marker.write_text("Levels=1,1\nLevelsDirs=full overview\nLevelsNum=2\n", encoding="utf-8")
        expect_code(lambda: run_plan(restored), "UNSUPPORTED_PYRAMID")
        expect_code(
            lambda: build_plan(environment, [inputs / "east.tif"], output / "子层任务", raster_mode="independent"),
            "UNSUPPORTED_PYRAMID",
        )
        marker.unlink()
        mosaic = output / "合成Mosaic索引"
        mosaic.mkdir()
        (mosaic / "index.shp").write_bytes(b"Synthetic blocked index; not a real shapefile.")
        (mosaic / "indexer.properties").write_text("UseExistingSchema=true\nTypeName=tiles\n", encoding="utf-8")
        expect_code(
            lambda: build_plan(environment, [mosaic / "index.shp"], output / "索引任务", raster_mode="independent"),
            "UNSUPPORTED_MOSAIC",
        )
        report = {
            "synthetic_only": True,
            "gdal_version": plan["capabilities"].get("version"),
            "unconfirmed_blocked_before_gdal": True,
            "confirmed_resource_count": len(plan["resources"]),
            "frozen_mode": restored["import_intent"]["raster_mode"],
            "source_membership_change_blocked": True,
            "new_ancestor_marker_blocked": True,
            "explicit_pyramid_tile_blocked": True,
            "explicit_mosaic_index_blocked": True,
        }
        if desktop:
            report["desktop"] = check_desktop(environment, inputs, output, font)
        after = {path.name: digest_file(path) for path in inputs.iterdir()}
        assert after == before
        assert counts["network_attempts"] == counts["client_creation_attempts"] == 0
        report.update(counts, source_hashes_unchanged=True)
        (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gdal-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New, disposable validation directory")
    parser.add_argument("--desktop", action="store_true", help="Exercise the real Qt background plan worker")
    parser.add_argument("--font", type=Path, help="Optional already-installed local font for headless screenshots")
    args = parser.parse_args()
    print(json.dumps(validate(args.gdal_bin, args.output, desktop=args.desktop, font=args.font), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
