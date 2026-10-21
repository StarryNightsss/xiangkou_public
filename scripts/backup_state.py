"""Create a consistent SQLite backup without stopping 巷口."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xiangkou.storage import StateDatabase


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", help="backup .sqlite3 path")
    parser.add_argument("--source", default=os.getenv("XIANGKOU_DATA_PATH") or
                        str(Path(__file__).resolve().parents[1] / "data" / "xiangkou.sqlite3"))
    args = parser.parse_args()
    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        parser.error(f"source database does not exist: {source}")
    print(StateDatabase(str(source)).backup(args.destination))


if __name__ == "__main__":
    main()
