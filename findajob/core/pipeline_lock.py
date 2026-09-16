"""One writing stage at a time.

Pipeline stages (`search`, `score`, `migrate`) write to SQLite in WAL mode.
Even in WAL mode, SQLite admits only one active writer at a time. To prevent sustained
write contention from exceeding SQLite's busy timeout and throwing `database is locked`,
all writing stages serialize on an exclusive flock managed centrally in `findajob.cli`.

The kernel drops the flock automatically when the holder process terminates or dies,
so a crashed stage cannot wedge subsequent runs.
"""

import fcntl
import sys
from collections.abc import Generator
from contextlib import contextmanager

from findajob.core.paths import PIPELINE_LOCK_PATH


@contextmanager
def hold(stage: str) -> Generator[None]:
    with open(PIPELINE_LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"{stage}: another pipeline stage holds the database; waiting.", file=sys.stderr)
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield
