"""Reproduce all four pipeline stages using two synthetic JSON fixtures."""
import argparse
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

try:
    from ._archive import zstd
except ImportError:
    from _archive import zstd

ROOT = Path(__file__).resolve().parents[1]


def make_archive(destination):
    """Package only the explicit public fixtures, never arbitrary local files."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in ("direct_track.json", "events_container.json"):
            content = (ROOT / "examples" / name).read_bytes()
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    if zstd is not None:
        destination.write_bytes(zstd.compress(buffer.getvalue()))
    elif shutil.which("zstd"):
        with destination.open("wb") as output:
            subprocess.run(["zstd", "-q", "-c"], input=buffer.getvalue(), stdout=output, check=True)
    else:
        raise RuntimeError("Нужен Python 3.14+ или команда zstd в PATH")


def build(output):
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Каталог уже содержит файлы; укажите новый --output")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "synthetic.tar.zst"
    make_archive(archive)
    commands = [
        ["tram_pilot_probe.py", str(archive), "--output", str(output / "profile")],
        ["tram_pilot_ingest.py", str(archive), "--output", str(output / "ingested"), "--format", "csv"],
        ["tram_pilot_incidents.py", "--input", str(output / "ingested"), "--output", str(output / "diagnostics")],
        ["tram_pilot_episodes.py", "--input", str(output / "diagnostics"), "--output", str(output / "episodes")],
    ]
    for script, *args in commands:
        subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args], check=True)
    print(f"Готовые синтетические результаты: {output / 'episodes'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/example"))
    args = parser.parse_args()
    try:
        build(args.output)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")
