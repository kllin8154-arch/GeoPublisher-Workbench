"""PyInstaller entry point; the optional GIS runtime is configured separately."""

from geopublisher.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
