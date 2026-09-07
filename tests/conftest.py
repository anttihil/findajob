"""Keep a test run out of the production database and log.

`careerradar.core.logger` opens `LOG_PATH` at import time, so merely importing the package
appended test records to the real `app.log` -- fixture circuit trips, throwaway migrations
and imports of modules that no longer exist all landed in the file the operator reads to
diagnose production.

This runs at import rather than in a fixture on purpose: pytest imports conftest before the
test modules, and by the time any fixture runs the handler is already open on whatever path
`paths.py` resolved.
"""

import os
import tempfile

os.environ["CAREERRADAR_LOG_PATH"] = os.path.join(tempfile.gettempdir(), "careerradar-tests.log")

# Same reasoning for the database. `tests/test_targets_api.py` drives the real targets API,
# and `prune_cells` disables cells rather than deleting them, so every run left permanent
# rows in the operator's `jobs.db` -- 84 orphan `test_city` cells were found there. A fresh
# file per run also stops tests from asserting against whatever the last scrape happened to
# collect. `Database.__init__` migrates the file, so an empty path is all that is needed.
os.environ["CAREERRADAR_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="careerradar-tests-"), "jobs.db"
)
