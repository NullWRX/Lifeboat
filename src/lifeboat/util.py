"""Small helpers shared by every part of Lifeboat."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterable, Iterator, Sequence

log = logging.getLogger("lifeboat")


class LifeboatError(Exception):
    """An error with a message meant for the user."""


class Unavailable(LifeboatError):
    """The destination is not reachable right now (drive unplugged, NAS down)."""


def is_root() -> bool:
    return os.geteuid() == 0


def require_root(action: str) -> None:
    if not is_root():
        raise LifeboatError(f"{action} needs root. Run it with sudo.")


def which(name: str) -> str | None:
    return shutil.which(name, path=os.environ.get("PATH", "") + ":/usr/sbin:/sbin:/usr/bin:/bin")


def run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    timeout: float | None = 60,
    input: str | bytes | None = None,
    env: dict | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command and capture its output. Raises LifeboatError on failure."""
    log.debug("run: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            list(cmd),
            input=input,
            capture_output=True,
            text=text,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise LifeboatError(f"Command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LifeboatError(f"Command timed out after {timeout}s: {' '.join(cmd)}") from exc
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() if text else ""
        raise LifeboatError(f"{' '.join(cmd)} failed ({proc.returncode}): {err}")
    return proc


def run_out(cmd: Sequence[str], **kw) -> str:
    """Run a command and return stdout, or '' if it fails or is missing."""
    try:
        return run(cmd, check=True, **kw).stdout
    except LifeboatError as exc:
        log.debug("%s", exc)
        return ""


def atomic_write(path: Path, data: str | bytes, mode: int = 0o644) -> None:
    """Write a file so readers never see a half-written version."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_json(path: Path, obj, mode: int = 0o644) -> None:
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True) + "\n", mode)


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return default


@contextlib.contextmanager
def file_lock(path: Path, blocking: bool = False) -> Iterator[None]:
    """Hold an exclusive flock on path for the duration of the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise LifeboatError("Another Lifeboat run is already using this job.") from exc
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)


def human_size(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def now() -> float:
    return time.time()


def is_within(path: str, parent: str) -> bool:
    """True if path equals parent or lies inside it (pure string check on normalized paths)."""
    path = os.path.normpath(path)
    parent = os.path.normpath(parent)
    if parent == "/":
        return True
    return path == parent or path.startswith(parent + "/")


def dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def minimize_paths(paths: Iterable[str]) -> list[str]:
    """Drop paths already covered by a parent in the list."""
    norm = sorted({os.path.normpath(p) for p in paths}, key=lambda p: (p.count("/"), p))
    kept: list[str] = []
    for p in norm:
        if not any(is_within(p, k) for k in kept):
            kept.append(p)
    return sorted(kept)
