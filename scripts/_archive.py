"""Stream TAR.ZST without extraction; use Python 3.14 or the zstd executable."""
from contextlib import contextmanager
import shutil
import subprocess
import tarfile
import tempfile

try:
    from compression import zstd
except ImportError:  # Python 3.10–3.13
    zstd = None


@contextmanager
def open_tar_zst(path):
    if zstd is not None:
        with zstd.open(path, "rb") as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                yield archive
            # A full scan must also validate the compressed frame trailer.
            while stream.read(1024 * 1024):
                pass
        return
    if shutil.which("zstd") is None:
        raise RuntimeError("Нужен Python 3.14+ или команда zstd в PATH")
    # A file avoids blocking on an unread stderr pipe for a corrupt archive.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            ["zstd", "-dc", "--", str(path)], stdout=subprocess.PIPE, stderr=errors
        )
        completed = False
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                yield archive
            while process.stdout.read(1024 * 1024):
                pass
            code = process.wait(timeout=30)
            if code:
                errors.seek(0)
                raise RuntimeError(f"zstd: код {code}: {errors.read(2000).decode('utf-8', 'replace')}")
            completed = True
        finally:
            process.stdout.close()
            if not completed and process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class StopScan(Exception):
    """Exit a limited scan without decompressing the remaining archive."""
