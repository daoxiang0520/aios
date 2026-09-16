from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class RuntimeAlreadyRunning(RuntimeError):
    """Raised when another process owns the runtime for the same database."""


class RuntimeProcessLock:
    """Small cross-platform process lock released automatically on process exit."""

    def __init__(self, database: Path):
        self.database = database
        self.path = database.with_name(f"{database.name}.runtime.lock")
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeAlreadyRunning(
                f"Another AIOS runtime already owns database {self.database}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> RuntimeProcessLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
