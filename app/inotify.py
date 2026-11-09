"""inotify wrapper, standard library only.

Linux inotify via ``ctypes`` on libc — no third-party package and no
``inotify-tools`` binary to install (the Docker image is
``python:3.12-slim`` and has neither).  The old PHP ``listen.sh``
used ``inotifywait``; this is the same kernel mechanism, called
directly.

Only the few calls orz needs are wrapped:
``inotify_init1``, ``inotify_add_watch``, ``inotify_rm_watch`` and
``read`` on the fd.  Events are parsed from the kernel's
``struct inotify_event`` (16-byte header + name).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import struct

# inotify event masks (linux/inotify.h)
IN_ACCESS = 0x00000001
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_ONLYDIR = 0x01000000

IN_CLOEXEC = 0o2000000
IN_NONBLOCK = 0o4000

# What orz watches for: a file/dir appearing, being written to, or
# being moved in.  IN_MODIFY is included so a slow copy keeps
# pushing its "stable" deadline back; IN_CLOSE_WRITE alone would
# miss a file that is still being written by a writer that never
# closes it promptly.
WATCH_MASK = (
    IN_CREATE | IN_MOVED_TO | IN_MOVED_FROM | IN_CLOSE_WRITE
    | IN_MODIFY | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF
)

_HEADER = struct.Struct("iIII")  # wd, mask, cookie, len


class Inotify:
    """One inotify instance; ``fileno()`` so ``select`` can wait on it."""

    def __init__(self) -> None:
        libname = ctypes.util.find_library("c")
        if not libname:
            raise OSError("libc not found, inotify unavailable")
        self._libc = ctypes.CDLL(libname, use_errno=True)
        self._libc.inotify_init1.restype = ctypes.c_int
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_rm_watch.restype = ctypes.c_int
        self._libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(),
                          "inotify_init1 failed", os.strerror(
                              ctypes.get_errno()))
        self.fd = fd
        self._closed = False

    def fileno(self) -> int:
        return self.fd

    def add_watch(self, path: str, mask: int = WATCH_MASK) -> int:
        wd = self._libc.inotify_add_watch(
            self.fd, os.fsencode(path), mask)
        if wd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"inotify_add_watch {path!r} failed",
                          os.strerror(err))
        return wd

    def rm_watch(self, wd: int) -> None:
        self._libc.inotify_rm_watch(self.fd, wd)

    def read_events(self) -> list[tuple[int, int, str]]:
        """All pending events as ``(wd, mask, name)``; ``[]`` when none.

        ``name`` is "" for events on the watched directory itself
        (e.g. IN_DELETE_SELF).  Raises OSError on a real read error;
        EAGAIN just means the queue drained.
        """
        try:
            data = os.read(self.fd, 64 * 1024)
        except BlockingIOError:
            return []
        events = []
        off = 0
        while off + _HEADER.size <= len(data):
            wd, mask, _cookie, nlen = _HEADER.unpack_from(data, off)
            off += _HEADER.size
            raw = data[off:off + nlen]
            off += nlen
            name = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
            events.append((wd, mask, name))
        return events

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self.fd)


def available() -> bool:
    """True when this kernel / libc can do inotify."""
    try:
        ino = Inotify()
    except OSError:
        return False
    ino.close()
    return True


def max_queued_events() -> int | None:
    """Current ``fs.inotify.max_queued_events`` (None when unreadable).

    The kernel default is 16384 events per inotify instance (orz
    has exactly one).  It is a host-wide kernel setting, not
    something a container can change — the host sets it with
    ``echo N > /proc/sys/fs/inotify/max_queued_events`` as root.
    """
    try:
        with open("/proc/sys/fs/inotify/max_queued_events",
                  encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None
