"""Canonical filesystem locations, derived once.

Every module used to recompute the repo root with its own
`os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`. That expression encodes
the module's own depth in the tree, so the package reorganization silently broke twelve
copies of it at once -- each one resolving to `careerradar/` instead of the repo root,
with no error until something tried to open a file that wasn't there.

One definition, imported everywhere. Moving a module between subpackages can no longer
change where it thinks the database is.
"""

import os

# careerradar/core/paths.py -> careerradar/core -> careerradar -> repo root
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

PACKAGE_ROOT = os.path.join(REPO_ROOT, "careerradar")
DATA_DIR = os.path.join(REPO_ROOT, "data")

DB_PATH = os.path.join(REPO_ROOT, "jobs.db")
# LangGraph checkpoints live in their own file: the interview graph writes a checkpoint per
# turn, and that write volume has no business sharing a WAL with the posting corpus.
GRAPH_DB_PATH = os.path.join(REPO_ROOT, "graphs.db")

CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")
ENV_PATH = os.path.join(REPO_ROOT, ".env")
LOG_PATH = os.path.join(REPO_ROOT, "app.log")
STATUS_PATH = os.path.join(REPO_ROOT, "sync_status.json")

ARCHIVE_DIR = os.path.join(REPO_ROOT, "raw_payloads")
DIGEST_DIR = os.path.join(REPO_ROOT, "digests")
FRONTEND_DIR = os.path.join(PACKAGE_ROOT, "web", "frontend")

