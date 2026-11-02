"""Chinese CLI sharing the same plan and execution APIs as the desktop UI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import Environment
from .plans import build_plan, load_plan


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="geopub", description="GeoServer 自动发布工作台：预检、计划、发布与结果核验。"
    )
    root.add_argument("--version", action="version", version=f"GeoPublisher Workbench {__version__}")
    sub = root.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="检查受控 GDAL 和服务器只读连接")
    doctor.add_argument("--environment", required=True, type=Path)
    doctor.add_argument("--offline", action="store_true", help="只检查本地运行时")
    plan = sub.add_parser("plan", help="离线预检并生成不可变发布计划，不写入服务器")
    plan.add_argument("sources", nargs="+", type=Path)
    plan.add_argument("--environment", required=True, type=Path)
    plan.add_argument("--output", required=True, type=Path)
    plan.add_argument("--strategy", choices=["fail", "skip", "new-version"], default="fail")
    plan.add_argument("--encoding", help="CPG 缺失时显式确认 GBK/UTF-8 等编码")
    plan.add_argument("--crs", help="明确赋予缺失的 CRS；不执行重投影")
    plan.add_argument("--sld", type=Path, help="单图层 SLD 路径")
    plan.add_argument("--sld-directory", type=Path, help="按 SHP/TIFF 文件名精确匹配批量 SLD")
    plan.add_argument("--name", help="单数据集的服务图层名称")
    plan.add_argument("--wfs", action="store_true", help="矢量同时验收 WFS")
    plan.add_argument(
        "--raster-mode",
        choices=["independent"],
        help="明确将多个 TIFF 分别发布为独立图层；不能绕过 Mosaic/Pyramid 结构阻断",
    )
    for verb, help_text in [
        ("run", "执行或恢复已确认的计划"),
        ("cleanup", "清理本任务拥有的服务配置，保留实际数据文件"),
    ]:
        command = sub.add_parser(verb, help=help_text)
        command.add_argument("plan", type=Path)
        command.add_argument("--journal", type=Path)
        command.add_argument("--yes", action="store_true", help="表示已审核并授权此计划及列出的操作")
    arcmap = sub.add_parser("arcmap-helper", help="导出离线 ArcMap 10.x MXD→SHP/LYR 辅助脚本")
    arcmap.add_argument("--output", type=Path, required=True)
    arcmap.add_argument("--demo", action="store_true", help="导出 ArcMap 中真正创建合成 MXD 的示例脚本")
    gdb = sub.add_parser("convert-gdb", help="离线批量转换本地 GDB 要素类为 SHP（附字段映射报告）")
    gdb.add_argument("gdb", type=Path, help="本机 .gdb 目录")
    gdb.add_argument("--output", required=True, type=Path, help="全新输出目录，不覆盖已有数据")
    gdb.add_argument("--gdal-bin", required=True, type=Path, help="受控 GDAL 3.13.3 bin 目录")
    gdb.add_argument("--include-annotation-bounds", action="store_true", help="允许导出注记边界，但不保留完整文字排版")
    gdb.add_argument("--layer", action="append", help="只处理指定 GDB 图层；可重复使用")
    gdb.add_argument("--preview", action="store_true", help="只读预览，不生成任何输出文件")
    status = sub.add_parser("status", help="查看本地任务记录")
    status.add_argument("--journal", type=Path, required=True)
    status.add_argument("--task")
    catalog = sub.add_parser("catalog", help="按人工核验字段映射生成目录 SQL，不连接数据库")
    catalog.add_argument("bindings", type=Path)
    catalog.add_argument("--mapping", type=Path, required=True)
    catalog.add_argument("--output", type=Path, required=True)
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            from .datasets import GdalRuntime

            environment = Environment.load(args.environment)
            result = {
                "runtime": GdalRuntime(
                    environment.gdal_bin,
                    expected_version=environment.gdal_version,
                    credential_env_names=(environment.username_env, environment.password_env),
                ).probe()
            }
            if not args.offline:
                with environment.client() as client:
                    result["geoserver"] = client.probe()
                    result["workspace_exists"] = client.get_workspace(environment.workspace) is not None
            _print(result)
            return 0
        if args.command == "arcmap-helper":
            from .arcgis_bridge import save_arcmap_helper

            target = save_arcmap_helper(args.output, demo=args.demo)
            _print({"status": "EXPORTED", "script": str(target), "requires": "ArcMap 10.x / ArcPy", "synthetic_demo": args.demo})
            return 0
        if args.command == "convert-gdb":
            from .datasets import DatasetError, GdalRuntime
            from .gdb_export import convert_gdb, inspect_gdb

            runtime = GdalRuntime(args.gdal_bin, expected_version="3.13.3")
            if args.preview:
                metadata = inspect_gdb(runtime, args.gdb)
                chosen = [x for x in metadata["layers"] if not args.layer or x["name"] in args.layer]
                if not chosen or (args.layer and not set(args.layer).issubset({x["name"] for x in chosen})):
                    raise DatasetError("GDB_LAYER_SELECTION_INVALID", "指定的图层名称不存在于当前 GDB。")
                _print({
                    "mode": "READ_ONLY_PREVIEW",
                    "source": Path(args.gdb).name,
                    "layer_count": len(chosen),
                    "feature_count": sum(int(x["feature_count"] or 0) for x in chosen),
                    "annotation_layers": sum(bool(x["annotation"]) for x in chosen),
                    "empty_layers": sum(not x["feature_count"] for x in chosen),
                    "warnings": sum(len(x["warnings"]) for x in chosen),
                    "layers": [
                        {"name": x["name"], "feature_count": x["feature_count"],
                         "geometry": x["geometry"], "crs": x["crs"],
                         "annotation": x["annotation"], "warnings": x["warnings"]}
                        for x in chosen
                    ],
                })
                return 0
            report = convert_gdb(
                runtime, args.gdb, args.output,
                include_annotations=args.include_annotation_bounds,
                only_layers=args.layer,
            )
            _print({
                "status": "LOCAL_CONVERSION_COMPLETE",
                "summary": report["summary"],
                "report_path": str(Path(args.output).absolute() / "conversion-report.json"),
                "source_unchanged": report["source_unchanged"],
                "requires_human_style_review": True,
            })
            return 3 if report["summary"].get("FAILED", 0) else 0
        if args.command == "plan":
            plan = build_plan(
                Environment.load(args.environment),
                args.sources,
                args.output,
                strategy=args.strategy,
                encoding=args.encoding,
                declared_crs=args.crs,
                sld=args.sld,
                sld_directory=args.sld_directory,
                target_name=args.name,
                enable_wfs=args.wfs,
                raster_mode=args.raster_mode,
            )
            _print(
                {
                    "task_id": plan["task_id"],
                    "ready": plan["ready"],
                    "plan_path": str(Path(plan["output_dir"]) / "plan.json"),
                    "raster_mode": plan["import_intent"]["raster_mode"],
                    "raster_count": plan["import_intent"]["raster_count"],
                    "resources": [
                        {
                            "target_name": r["target_name"],
                            "kind": r["kind"],
                            "blocking_errors": r["blocking_errors"],
                            "warnings": r["warnings"],
                        }
                        for r in plan["resources"]
                    ],
                }
            )
            return 0 if plan["ready"] else 2
        if args.command in {"run", "cleanup"}:
            from .engine import rollback_plan, run_plan

            plan = load_plan(args.plan)
            summary = {
                "operation": args.command,
                "target": plan["environment"]["base_url"],
                "workspace": plan["environment"]["workspace"],
                "resources": [r["target_name"] for r in plan["resources"]],
                "strategy": plan["strategy"],
                "raster_mode": plan.get("import_intent", {}).get("raster_mode", "legacy-unrecorded"),
                "raster_count": sum(r["kind"] == "geotiff" for r in plan["resources"]),
            }
            if not args.yes:
                _print(summary)
                if not sys.stdin.isatty() or input(
                    "确认上述目标和操作，输入 PUBLISH 或 CLEANUP："
                ).strip() != ("PUBLISH" if args.command == "run" else "CLEANUP"):
                    _print(
                        {"status": "NOT_AUTHORIZED", "message": "未执行。审核计划后，交互确认或使用 --yes。"}
                    )
                    return 2
            result = (
                run_plan(plan, args.journal) if args.command == "run" else rollback_plan(plan, args.journal)
            )
            _print(result)
            return 0 if result["status"] in {"SUCCEEDED", "SKIPPED", "ROLLED_BACK"} else 3
        if args.command == "status":
            from .journal import Journal

            if not args.journal.is_file():
                _print({"code": "JOURNAL_MISSING", "message": "任务数据库不存在。"})
                return 2
            with Journal(args.journal) as journal:
                _print(
                    {
                        "task": journal.get_task(args.task),
                        "resources": journal.get_resources(args.task),
                        "operations": journal.operations(args.task),
                    }
                    if args.task
                    else journal.list_tasks()
                )
            return 0
        if args.command == "catalog":
            from .catalog import generate_catalog

            result = generate_catalog(
                json.loads(args.bindings.read_text(encoding="utf-8-sig")),
                json.loads(args.mapping.read_text(encoding="utf-8-sig")),
                args.output,
            )
            _print(result)
            return 0
    except KeyboardInterrupt:
        _print(
            {
                "status": "INTERRUPTED",
                "message": "客户端已中断。写请求可能仍在服务端执行；再次运行会先核对任务记录。",
            }
        )
        return 130
    except Exception as exc:
        code = getattr(exc, "code", "LOCAL_ERROR")
        # Raw unexpected exception text may contain response bodies or connection details.
        message = str(exc) if hasattr(exc, "code") else "本地操作失败，请检查文件、运行时和任务配置。"
        _print({"code": code, "message": message})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
