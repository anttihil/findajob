"""Persist and load profile versions.

Profiles are append-only. Rebuilding writes a new version and moves the `is_active` flag;
it never edits an old row. Verdicts record the `profile_version` they were produced under,
so "why did this posting's score change?" stays answerable after a rebuild.
"""

import json
from datetime import datetime, timezone

from careerradar.core.database import Database
from careerradar.profile.models import Profile
from careerradar.profile.render import render_profile


def _now():
    return datetime.now(timezone.utc).isoformat()


def next_version(conn):
    row = conn.execute("SELECT MAX(version) FROM profiles").fetchone()
    return (row[0] or 0) + 1


def save_profile(profile: Profile, model, documents=None, turns=None, db=None,
                 taxonomy=None):
    """Write a new profile version and make it active. Returns the version number.

    Skill keys are canonicalized against the taxonomy on the way in, so a profile can
    never be persisted with keys the keyword layer cannot look up. See
    `profile/canonicalize.py` for why this is an invariant here rather than a prompt.
    """
    from careerradar.profile.canonicalize import canonicalize_profile
    from careerradar.profile.ingest import corpus_hash

    if taxonomy is None:
        from careerradar.taxonomy.skills import load_taxonomy
        taxonomy = load_taxonomy()
    profile, _report = canonicalize_profile(profile, taxonomy)

    owned = db is None
    db = db or Database()
    conn = db.conn
    try:
        version = next_version(conn)
        summary = render_profile(profile)

        # Clearing first: `idx_profiles_one_active` is a partial unique index on
        # is_active = 1, so activating a second profile without this is an IntegrityError.
        conn.execute("UPDATE profiles SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            """
            INSERT INTO profiles
                (version, created_at, is_active, model, profile_json, summary_text, corpus_hash)
            VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (
                version,
                _now(),
                model,
                profile.model_dump_json(),
                summary,
                corpus_hash(documents) if documents else None,
            ),
        )

        for document in documents or []:
            conn.execute(
                """
                INSERT OR REPLACE INTO profile_documents
                    (profile_version, path, kind, sha256, chars, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (version, document.path, document.kind, document.sha256,
                 len(document.text), _now()),
            )

        for seq, turn in enumerate(turns or []):
            conn.execute(
                """
                INSERT OR REPLACE INTO interview_turns
                    (profile_version, seq, topic, question, answer, asked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (version, seq, turn.get("topic"), turn.get("question"),
                 turn.get("answer"), turn.get("asked_at") or _now()),
            )

        conn.commit()
        return version
    finally:
        if owned:
            db.close()


def load_active(db=None):
    """The active profile, or None. Returns (version, Profile, summary_text)."""
    owned = db is None
    db = db or Database()
    try:
        row = db.conn.execute(
            "SELECT version, profile_json, summary_text FROM profiles WHERE is_active = 1"
        ).fetchone()
        if row is None:
            return None
        return row["version"], Profile.model_validate_json(row["profile_json"]), row["summary_text"]
    finally:
        if owned:
            db.close()


def load_active_row(db=None):
    """The active profile row as a dict, for the API. None if no profile exists."""
    owned = db is None
    db = db or Database()
    try:
        row = db.conn.execute(
            """
            SELECT version, created_at, model, profile_json, summary_text, corpus_hash
              FROM profiles WHERE is_active = 1
            """
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["profile"] = json.loads(record.pop("profile_json"))
        record["documents"] = [
            dict(r)
            for r in db.conn.execute(
                "SELECT path, kind, sha256, chars FROM profile_documents "
                "WHERE profile_version = ? ORDER BY path",
                (row["version"],),
            )
        ]
        return record
    finally:
        if owned:
            db.close()


def list_versions(db=None):
    owned = db is None
    db = db or Database()
    try:
        return [
            dict(r)
            for r in db.conn.execute(
                """
                SELECT p.version, p.created_at, p.is_active, p.model, p.corpus_hash,
                       (SELECT COUNT(*) FROM interview_turns t
                         WHERE t.profile_version = p.version) AS turns,
                       (SELECT COUNT(*) FROM profile_documents d
                         WHERE d.profile_version = p.version) AS documents
                  FROM profiles p ORDER BY p.version DESC
                """
            )
        ]
    finally:
        if owned:
            db.close()


def corpus_changed(documents, db=None):
    """Whether the corpus differs from what the active profile was built on.

    None means there is no active profile to compare against.
    """
    from careerradar.profile.ingest import corpus_hash

    owned = db is None
    db = db or Database()
    try:
        row = db.conn.execute(
            "SELECT corpus_hash FROM profiles WHERE is_active = 1"
        ).fetchone()
        if row is None or row["corpus_hash"] is None:
            return None
        return row["corpus_hash"] != corpus_hash(documents)
    finally:
        if owned:
            db.close()
