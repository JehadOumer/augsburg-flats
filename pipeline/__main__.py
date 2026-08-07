"""Allow `python -m pipeline` as an alias for export."""

from pipeline.export_listings import main

if __name__ == "__main__":
    raise SystemExit(main())
