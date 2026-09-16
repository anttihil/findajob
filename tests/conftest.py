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

os.environ["FIND_A_JOB_LOG_PATH"] = os.path.join(tempfile.gettempdir(), "findajob-tests.log")

# Use a fresh database so tests cannot modify or depend on the operator's database.
os.environ["FIND_A_JOB_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="findajob-tests-"), "jobs.db"
)
