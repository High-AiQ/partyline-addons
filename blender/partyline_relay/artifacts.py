"""Rendered images waiting for the helper: files in one private temp directory, named by random ids.

Plain Python (no bpy), so the HTTP threads can serve and drop them. An artifact belongs to the session that
made it, is handed over once, and is deleted when it is fetched, when its session ends or when the add-on stops.
"""
import secrets
import shutil
import tempfile
import threading
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024   # larger files are refused rather than streamed through the helper
KEEP = 8                       # unfetched artifacts per session; older ones are deleted first


class Artifacts:
    def __init__(self, root=None):
        self.root = Path(root) if root else None
        self.files = {}  # artifact id -> (session id, path), oldest first
        self.lock = threading.Lock()

    def new(self, session_id, suffix=".png"):
        """(artifact id, path to write); the file is registered as soon as the caller creates it."""
        with self.lock:
            if self.root is None:
                self.root = Path(tempfile.mkdtemp(prefix="partyline-relay-"))
            self.root.mkdir(parents=True, exist_ok=True)
            artifact = secrets.token_hex(12)
            self.files[artifact] = (session_id, self.root / (artifact + suffix))
            mine = [key for key, (owner, _) in self.files.items() if owner == session_id]
            for old in mine[:-KEEP]:
                self._remove(old)
            return artifact, self.files[artifact][1]

    def _remove(self, artifact):
        _, path = self.files.pop(artifact, (None, None))
        if path is not None:
            path.unlink(missing_ok=True)

    def discard(self, artifact, path=None):
        """Forget an artifact and delete its file. The producer passes its own path: a session that ended while
        the image was still being written has already dropped the entry, but the file appears afterwards."""
        with self.lock:
            self._remove(artifact)
        if path is not None:
            Path(path).unlink(missing_ok=True)

    def has(self, artifact):
        with self.lock:
            return artifact in self.files

    def take(self, session_id, artifact):
        """The bytes, once: (status, data or message). 404 for an unknown id or another session's."""
        with self.lock:
            entry = self.files.get(artifact)
            if entry is None or entry[0] != session_id:
                return 404, b"unknown artifact"
            path = entry[1]
            try:
                size = path.stat().st_size
                if size > MAX_BYTES:
                    return 413, b"artifact is too large"
                data = path.read_bytes()
            except OSError:
                return 404, b"artifact is gone"
            finally:
                self._remove(artifact)
            return 200, data

    def drop(self, session_id):
        with self.lock:
            for artifact in [key for key, (owner, _) in self.files.items() if owner == session_id]:
                self._remove(artifact)

    def clear(self):
        with self.lock:
            self.files.clear()
            if self.root is not None:
                shutil.rmtree(self.root, ignore_errors=True)
                self.root = None
