"""Atomic, mode-pinned file replacement shared by the camera-control service."""

import os
import secrets


def atomic_write(path, body: bytes, mode: int) -> None:
    """Replace ``path`` with ``body`` at exactly ``mode``, or leave it untouched."""
    path = os.path.abspath(os.fspath(path))
    parent, name = os.path.split(path)
    temporary = os.path.join(parent, f".{name}.new.{os.getpid()}.{secrets.token_hex(8)}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _fsync_directory(path) -> None:
    try:
        descriptor = os.open(path, os.O_DIRECTORY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
