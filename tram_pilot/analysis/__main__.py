"""Запуск расчёта поверх уже импортированных Track."""
import argparse
import json
from pathlib import Path

from ..database.connection import connect, load_env_file
from .pipeline import AnalysisOptions, AnalysisPipeline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--gap-seconds", type=float, default=30)
    parser.add_argument("--bridge-seconds", type=float, default=90)
    args = parser.parse_args()
    load_env_file(args.env_file)
    with connect() as connection:
        result = AnalysisPipeline(connection, AnalysisOptions(
            args.gap_seconds, args.bridge_seconds)).run()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
