"""Bounded filesystem snapshots. Never follow directory links or open devices."""

import hashlib
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path

MAX_FILE = 512 * 1024
MAX_FILES = 3000
MAX_TOTAL = 16 * 1024 * 1024
EXCLUDED_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
                 ".next", ".cache", "dist", "build", "coverage", ".hermes", ".ssh"}
SECRET_NAMES = {".env", ".npmrc", ".pypirc", "credentials", "credentials.json", "secrets.json",
                "id_rsa", "id_ed25519", "id_ecdsa"}


def excluded(path):
    p = Path(path)
    name = p.name.lower()
    return (any(part.lower() in EXCLUDED_DIRS for part in p.parts)
            or name in SECRET_NAMES or name.startswith(".env.")
            or name.endswith((".pem", ".key", ".p12", ".pfx")))


def snapshot(path, blocked=None):
    path = Path(path)
    if excluded(path) or (blocked and blocked(str(path))):
        return {"kind": "excluded"}, None
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return {"kind": "unsupported"}, None
        meta = {"size": info.st_size, "mode": stat.S_IMODE(info.st_mode)}
        if info.st_size > MAX_FILE:
            return dict(meta, kind="large", mtime=info.st_mtime_ns), None
        # O_NONBLOCK avoids hanging if a regular file is replaced with a FIFO.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                return {"kind": "unsupported"}, None
            data = stream.read(MAX_FILE + 1)
            final = os.fstat(stream.fileno())
        if len(data) > MAX_FILE or (opened.st_mtime_ns, opened.st_size) != (final.st_mtime_ns, final.st_size):
            return {"kind": "unstable"}, None
        digest = hashlib.sha256(data).hexdigest()
        meta = {"size": len(data), "mode": stat.S_IMODE(final.st_mode), "hash": digest}
        try:
            if b"\x00" in data:
                raise UnicodeError()
            data.decode("utf-8")
        except UnicodeError:
            return dict(meta, kind="binary"), None
        return dict(meta, kind="text"), data
    except FileNotFoundError:
        return {"kind": "absent"}, None
    except (OSError, ValueError):
        return {"kind": "unreadable"}, None


def candidates(root):
    """Use Git's ignore rules when available, with a bounded filesystem fallback."""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        with tempfile.TemporaryFile() as output:
            run = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others",
                                  "--exclude-standard", "-z"], stdout=output, stderr=subprocess.DEVNULL,
                                 timeout=3, **options)
            if run.returncode == 0:
                if output.tell() > 2 * 1024 * 1024:
                    raise ValueError("Workspace file list exceeds the capture limit")
                output.seek(0)
                for raw in dict.fromkeys(output.read().split(b"\x00")):
                    if raw:
                        yield root / os.fsdecode(raw)
                return
    except (OSError, subprocess.TimeoutExpired):
        pass
    def walk_error(error):
        raise error
    for base, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = sorted(d for d in dirs if d.lower() not in EXCLUDED_DIRS
                         and not (Path(base) / d).is_symlink())
        for name in sorted(files):
            yield Path(base) / name


def workspace(root, blocked=None):
    captured, blobs, notes = {}, {}, []
    started, total = time.monotonic(), 0
    try:
        for index, path in enumerate(candidates(root)):
            if index >= MAX_FILES or total >= MAX_TOTAL or time.monotonic() - started > 4:
                notes.append("Workspace capture reached its file, byte or time limit; coverage is partial.")
                break
            if excluded(path):
                continue
            # Ignore a link at any directory level, including Git-tracked paths.
            if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root.parent):
                continue
            meta, data = snapshot(path, blocked)
            captured[str(path)] = meta
            if data is not None:
                blobs[meta["hash"]] = data
                total += len(data)
    except (OSError, ValueError) as exc:
        notes.append(f"Workspace capture is partial: {exc}")
    return captured, blobs, notes
