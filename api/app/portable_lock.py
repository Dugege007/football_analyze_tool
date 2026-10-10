"""跨平台文件锁（替代只能在 Linux 和 macOS 上使用的 fcntl 模块）。

用法与 fcntl.flock 保持一致：
    from app.portable_lock import flock, LOCK_EX, LOCK_NB, LOCK_UN
    flock(file_object, LOCK_EX | LOCK_NB)

- 在 Linux 和 macOS 上直接调用 fcntl.flock。
- 在 Windows 上使用标准库 msvcrt.locking，锁定文件开头的一个字节。
- 非阻塞模式下锁已被其他进程占用时，统一抛出 BlockingIOError，与 fcntl 的行为一致。
"""
from __future__ import annotations

import os
import time

try:  # Linux 与 macOS
    import fcntl as _fcntl

    LOCK_EX = _fcntl.LOCK_EX
    LOCK_NB = _fcntl.LOCK_NB
    LOCK_UN = _fcntl.LOCK_UN

    def flock(f, op: int) -> None:
        _fcntl.flock(f, op)

except ImportError:  # Windows
    import msvcrt

    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def flock(f, op: int) -> None:
        fd = f.fileno()
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if op & LOCK_UN:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                return
            if op & LOCK_NB:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as e:
                    raise BlockingIOError(str(e)) from e
                return
            while True:  # 阻塞模式：一直重试直到拿到锁
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    time.sleep(0.05)
        finally:
            os.lseek(fd, pos, os.SEEK_SET)
