#!/usr/bin/env bash
# Pull the achievements log in from the `resume` repo.
#
#   scripts/sync_corpus.sh [path-to-resume-repo]    (default: ../resume)
#
# This used to rsync the whole resumes/ directory with --delete, because the profile was
# built by scanning that directory. It no longer is: `profile.corpus` in config.yaml names
# the documents explicitly, and the tailored resumes in the `resume` repo are written for
# SUBMITTING to employers -- a different kind of document from evidence about what someone
# can actually do. Six LLM-generated variants were being read as evidence, and their
# inflated skill lists became skill levels and then scores.
#
# --delete is gone too. It deleted anything in resumes/ that the source repo did not have,
# which includes hand-curated documents that live only here.

set -euo pipefail

SRC="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/../resume}"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -f "$SRC/achievements.md" ]; then
    echo "error: no achievements.md under $SRC" >&2
    echo "usage: scripts/sync_corpus.sh [path-to-resume-repo]" >&2
    exit 1
fi

cp "$SRC/achievements.md" "$DEST/achievements.md"
echo "synced achievements.md from $SRC"

# Report on the configured corpus rather than on a directory listing, so a document that is
# named but missing is visible here instead of at the next `profile build`.
cd "$DEST"
uv run python - <<'PY'
from careerradar.profile.ingest import CorpusError, collect_documents

try:
    documents = collect_documents()
except CorpusError as exc:
    print(f"\ncorpus problem:\n{exc}")
    raise SystemExit(1)

print("\nconfigured corpus:")
for document in documents:
    print(f"  {document.kind:<14} {document.path}  ({len(document.text):,} chars)")
PY
