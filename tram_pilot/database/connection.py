"""Настройки подключения из окружения и необязательного файла .env."""
from dataclasses import dataclass, field
import os
from pathlib import Path

ENV_KEYS = {"PGHOST", "PGPORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"}


def load_env_file(path: Path = Path(".env")) -> None:
    """Читает KEY=VALUE; уже заданные переменные окружения имеют приоритет."""
    if not path.exists():
        return
    for index, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in ENV_KEYS:
            raise ValueError(f"{path.name}:{index}: ожидалось известное имя переменной и '='")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)

    @classmethod
    def from_env(cls):
        password = os.environ.get("POSTGRES_PASSWORD", "")
        if not password:
            raise ValueError("Задайте POSTGRES_PASSWORD в .env или окружении")
        try:
            port = int(os.environ.get("PGPORT", "5432"))
        except ValueError as exc:
            raise ValueError("PGPORT должен быть числом") from exc
        if not 1 <= port <= 65535:
            raise ValueError("PGPORT должен быть от 1 до 65535")
        return cls(os.environ.get("PGHOST", "127.0.0.1"), port,
                   os.environ.get("POSTGRES_DB", "tram_pilot"),
                   os.environ.get("POSTGRES_USER", "tram_pilot"), password)


def connect(config: DatabaseConfig | None = None):
    import psycopg
    from psycopg.rows import dict_row

    config = config or DatabaseConfig.from_env()
    return psycopg.connect(host=config.host, port=config.port, dbname=config.dbname,
                           user=config.user, password=config.password, connect_timeout=5,
                           options="-c timezone=UTC", row_factory=dict_row)
