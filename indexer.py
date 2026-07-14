"""CLI: python indexer.py [--only ConfigName] [--config path/to/config.json] [--stats]"""
import argparse
import json
import sys
import time
from pathlib import Path

import db
import parser as p


def main() -> None:
    arg_parser = argparse.ArgumentParser(description="Index 1C configurations into SQLite")
    arg_parser.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    arg_parser.add_argument("--only", metavar="CONFIG_NAME", help="Re-index only this config")
    arg_parser.add_argument("--stats", action="store_true", help="Show index stats and exit")
    args = arg_parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    db_path = config["db_path"]
    db.ensure_schema(db_path)
    conn = db.get_connection(db_path)

    if args.stats:
        rows = conn.execute("SELECT * FROM index_runs ORDER BY config_name").fetchall()
        if not rows:
            print("Index is empty.")
        for row in rows:
            print(f"  {row['config_name']:20s}  {row['obj_count']:5d} objects  "
                  f"{row['file_count']:5d} files  indexed {row['indexed_at']}")
        indexed_names = {row["config_name"] for row in rows}
        not_indexed = [c["name"] for c in config["configs"] if c["name"] not in indexed_names]
        if not_indexed:
            print("Не проиндексированы:")
            for name in not_indexed:
                print(f"  {name}")
        conn.close()
        return

    configs = config["configs"]
    if args.only:
        configs = [c for c in configs if c["name"] == args.only]
        if not configs:
            print(f"Config '{args.only}' not found in config.json", file=sys.stderr)
            sys.exit(1)

    for cfg in configs:
        path = Path(cfg["path"])
        if not path.exists():
            print(f"SKIP {cfg['name']}: path does not exist: {path}")
            continue
        print(f"Indexing {cfg['name']} from {path}")
        start = time.monotonic()
        obj_count, file_count = p.index_config(
            conn, cfg, on_progress=lambda msg: print(f"  {msg}", flush=True)
        )
        elapsed = time.monotonic() - start
        print(f"  Done: {obj_count} objects, {file_count} modules in {elapsed:.1f}s")

    conn.close()


if __name__ == "__main__":
    main()
