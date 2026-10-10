"""跨平台文件锁：非阻塞加锁冲突时必须抛出 BlockingIOError，解锁后可以再次加锁。"""
import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import portable_lock as pl  # noqa: E402


def _try_lock(path: str, q) -> None:
    with open(path, "w") as f:
        try:
            pl.flock(f, pl.LOCK_EX | pl.LOCK_NB)
            q.put("locked")
        except BlockingIOError:
            q.put("blocked")


def test_lock_conflict_and_release(tmp_path):
    p = str(tmp_path / "daemon.lock")
    f = open(p, "w")
    pl.flock(f, pl.LOCK_EX | pl.LOCK_NB)
    q = mp.get_context("spawn").Queue()
    proc = mp.get_context("spawn").Process(target=_try_lock, args=(p, q))
    proc.start(); proc.join(30)
    assert q.get(timeout=5) == "blocked"
    pl.flock(f, pl.LOCK_UN)
    f.close()
    proc = mp.get_context("spawn").Process(target=_try_lock, args=(p, q))
    proc.start(); proc.join(30)
    assert q.get(timeout=5) == "locked"
