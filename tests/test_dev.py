"""Exercise CLI dev initialization in fresh interpreters, before paths are imported."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def _start_dev(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    # Deliberately conflicting inherited paths must not redirect dev writes.
    environment.update(
        FIND_A_JOB_HOME=str(root / "production"),
        FIND_A_JOB_DB_PATH=str(root / "production.db"),
        FIND_A_JOB_LOG_PATH=str(root / "production.log"),
        FIND_A_JOB_CONFIG_LOCAL_PATH=str(root / "production.yaml"),
        FIND_A_JOB_RESUMES_DIR=str(root / "production-resumes"),
        FIND_A_JOB_AUTH_TOKEN="production-secret",
    )
    script = """
import json, logging, logging.config, os, sys
from pathlib import Path
import uvicorn
from findajob import cli

root = Path(sys.argv[1])
cli.__file__ = str(root / 'findajob' / 'cli.py')

def serve(app, **kwargs):
    from findajob.core import paths
    from findajob.core.config import load_config, save_config
    from findajob.core.database import Database
    from findajob.core.logger import get_logger
    from findajob.core.scheduler_preferences import get_scheduler_preferences
    assert app == 'findajob.web.app:app'
    assert kwargs['port'] == 8123
    assert kwargs['reload'] is False
    assert not os.environ.get('FIND_A_JOB_AUTH_TOKEN')
    logging.config.dictConfig(kwargs['log_config'])
    get_logger().info('dev application log check')
    logging.getLogger('uvicorn.error').info('dev server log check')
    logging.getLogger('uvicorn.access').info('dev request log check')
    assert load_config()['scoring']['concurrency'] == 3
    save_config({'scoring': {'concurrency': 3}})
    db = Database()
    try:
        assert get_scheduler_preferences(db.conn)['enabled'] is False
        db.conn.execute('CREATE TABLE IF NOT EXISTS dev_marker (value TEXT)')
        row = db.conn.execute('SELECT value FROM dev_marker').fetchone()
        marker = row[0] if row else 'empty'
        db.conn.execute('DELETE FROM dev_marker')
        db.conn.execute("INSERT INTO dev_marker VALUES ('saved dev edit')")
        db.conn.commit()
    finally:
        db.close()
    print(json.dumps({'db': paths.DB_PATH, 'log': paths.LOG_PATH,
                      'resumes': paths.RESUMES_DIR, 'marker': marker}))

uvicorn.run = serve
raise SystemExit(cli.main(['start', '--dev', '--port', '8123']))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize("snapshot", [True, False])
def test_dev_isolation_seed_persistence_and_logging(tmp_path: Path, snapshot: bool) -> None:
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "config.yaml").write_text("scoring:\n  concurrency: 3\n")
    production = tmp_path / "prod_jobs.db"
    if snapshot:
        with sqlite3.connect(production) as conn:
            conn.execute("CREATE TABLE dev_marker (value TEXT)")
            conn.execute("INSERT INTO dev_marker VALUES ('production snapshot')")
        before = hashlib.sha256(production.read_bytes()).digest()

    first = _start_dev(tmp_path)
    home = tmp_path / ".dev"
    assert first["db"] == str(home / "data/jobs.db")
    assert first["log"] == str(home / "dev.log")
    assert first["resumes"] == str(home / "resumes")
    assert first["marker"] == ("production snapshot" if snapshot else "empty")
    second = _start_dev(tmp_path)
    assert second["marker"] == "saved dev edit"
    log = (home / "dev.log").read_text()
    for message in ("dev application log check", "dev server log check", "dev request log check"):
        assert log.count(message) == 2
    assert (home / "config/config.local.yaml").exists()
    assert not (tmp_path / "production").exists()
    assert not (tmp_path / "production.db").exists()
    assert not (tmp_path / "production.log").exists()
    assert not (tmp_path / "production.yaml").exists()
    if snapshot:
        assert hashlib.sha256(production.read_bytes()).digest() == before
