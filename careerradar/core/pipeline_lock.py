"""One writing stage at a time.

`search`, `score` and `research` run on independent systemd timers (deploy/*.timer): a
scrape takes ~20 minutes and scoring fires every half hour, so two stages overlap by
design. SQLite in WAL mode still admits one writer, and sustained contention between two
stages outlived the 30s busy_timeout and surfaced as `database is locked`. The stages are
steps of one pipeline and gain nothing from running together, so the second one queues.

A context manager rather than a process-lifetime lock, because the dashboard runs a sync
in-process as a background task: a web server that acquired and never released would hold
the pipeline shut until it restarted. The kernel also drops an flock when the holder dies,
so a crashed stage cannot wedge the next one -- unlike the sync_status.json lock, which
needs the stale-lock timeout in status_manager.py.

Waiting is unbounded on purpose. Every unit sets TimeoutStartSec, which is the bound that
already exists; a second one here would only disagree with it.
"""

import fcntl
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from careerradar.core.paths import PIPELINE_LOCK_PATH


@contextmanager
def hold(stage: str) -> Iterator[None]:
    with open(PIPELINE_LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"{stage}: another pipeline stage holds the database; waiting.", file=sys.stderr)
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield
