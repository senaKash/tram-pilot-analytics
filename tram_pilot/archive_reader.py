"""Поток документов из каталога JSON или TAR.ZST без распаковки архива."""
import json
from pathlib import Path

from scripts._archive import StopScan, open_tar_zst


def read_documents(path: Path, max_files: int, max_json_mb: int = 5):
    """Даёт (имя, объект, ошибка), удерживая в памяти лишь один JSON."""
    if max_files < 1 or max_json_mb < 1:
        raise ValueError("Лимиты файлов и размера JSON должны быть положительными")
    path = Path(path)
    if path.is_dir():
        files = sorted(path.glob("*.json"))
        for file in files[:max_files]:
            if file.stat().st_size > max_json_mb * 1024 * 1024:
                yield file.name, None, "Размер JSON превышает лимит"
            else:
                yield file.name, *_parse(file)
        return
    if not path.is_file() or not path.name.endswith(".tar.zst"):
        raise ValueError("Нужен каталог JSON или локальный файл .tar.zst")
    checked = 0
    try:
        with open_tar_zst(path) as archive:
            for member in archive:
                if not member.isfile():
                    continue
                if checked >= max_files:
                    raise StopScan()
                checked += 1
                if member.size > max_json_mb * 1024 * 1024:
                    yield member.name, None, "Размер JSON превышает лимит"
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    yield member.name, None, "JSON недоступен в архиве"
                    continue
                with stream:
                    yield member.name, *_parse(stream)
    except StopScan:
        pass


def _parse(source):
    def invalid_constant(_value):
        raise ValueError("JSON содержит NaN или Infinity")

    try:
        if isinstance(source, Path):
            with source.open(encoding="utf-8-sig") as stream:
                obj = json.load(stream, parse_constant=invalid_constant)
        else:
            obj = json.load(source, parse_constant=invalid_constant)
        if not isinstance(obj, dict):
            return None, "Корень JSON должен быть объектом"
        return obj, None
    except (ValueError, UnicodeError, RecursionError) as exc:
        return None, f"Некорректный JSON ({type(exc).__name__})"
