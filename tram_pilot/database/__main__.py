"""Команды миграции, демонстрационного импорта и чтения PostgreSQL."""
import argparse
import json
from pathlib import Path

from .connection import connect, load_env_file
from .repository import (insert_tracks, migrate, read_events, read_import_runs,
                         read_tracks)
from .importer import BatchImporter
from .analysis_repository import publish_analysis
from ..normalize import load_demo


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Применить SQL-миграции")
    publication = commands.add_parser("publish-analysis", help="Опубликовать завершённый расчёт")
    publication.add_argument("--run-id", type=int, required=True)
    demo = commands.add_parser("import-demo", help="Импортировать небольшой набор JSON")
    demo.add_argument("--input", type=Path, default=Path("examples"))
    demo.add_argument("--max-records", type=int, default=1000)
    read = commands.add_parser("read", help="Прочитать сохранённые Track")
    read.add_argument("--limit", type=int, default=10)
    read.add_argument("--loco-number")
    bulk = commands.add_parser("import-batch", help="Потоковый COPY-импорт JSON или TAR.ZST")
    bulk.add_argument("--source", type=Path, required=True)
    bulk.add_argument("--max-files", type=int, default=20000)
    bulk.add_argument("--batch-size", type=int, default=100)
    bulk.add_argument("--max-json-mb", type=int, default=5)
    for name in ("read-events", "runs"):
        command = commands.add_parser(name)
        command.add_argument("--limit", type=int, default=10)
        if name == "read-events":
            command.add_argument("--loco-number")
    args = parser.parse_args(argv)
    try:
        load_env_file(args.env_file)
        if args.command == "import-demo":
            records, skipped = load_demo(args.input, args.max_records)
        if args.command in {"read", "read-events", "runs"} and not 1 <= args.limit <= 1000:
            raise ValueError("--limit должен быть от 1 до 1000")
        with connect() as connection:
            if args.command == "migrate":
                result = {"applied": migrate(connection)}
            elif args.command == "publish-analysis":
                publish_analysis(connection, args.run_id)
                result = {"published_analysis_run_id": args.run_id}
            elif args.command == "import-demo":
                result = {"track_records": len(records), "skipped_non_track": skipped,
                          **insert_tracks(connection, records)}
            elif args.command == "import-batch":
                result = BatchImporter(connection, args.source, args.batch_size,
                                       args.max_files, args.max_json_mb).run()
            elif args.command == "read":
                result = read_tracks(connection, args.limit, args.loco_number)
            elif args.command == "read-events":
                result = read_events(connection, args.limit, args.loco_number)
            else:
                result = read_import_runs(connection, args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")
    except ImportError:
        parser.exit(1, "Установите зависимости: python -m pip install -r requirements.txt\n")
    except Exception as exc:
        # Do not echo libpq's connection settings or credentials into logs.
        import psycopg
        if isinstance(exc, psycopg.Error):
            parser.exit(1, f"Ошибка PostgreSQL ({type(exc).__name__}). Проверьте сервер и выполните migrate.\n")
        raise


if __name__ == "__main__":
    main()
