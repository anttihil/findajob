#!/usr/bin/env bash
# Copy the resume corpus in from the `resume` repo.
#
# The corpus -- six tailored resumes plus current_resume.md and achievements.md -- is what
# build_profile() turns into skill levels, so it decides every match score. It is gitignored
# here on purpose: these are personal career documents and this repo is meant to be free of
# them. The `resume` repo is the source of truth; run this after editing a resume there, and
# after every fresh clone.
#
#   scripts/sync_corpus.sh [path-to-resume-repo]    (default: ../resume)

set -euo pipefail

SRC="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/../resume}"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -d "$SRC/resumes" ]; then
    echo "error: no resumes/ under $SRC" >&2
    echo "usage: scripts/sync_corpus.sh [path-to-resume-repo]" >&2
    exit 1
fi

# --delete so a resume deleted upstream stops contributing skill evidence here. Only the
# markdown is read; the PDFs come along because they are cheap and keeping the directory
# identical makes it obvious when the two have diverged.
rsync -a --delete "$SRC/resumes/" "$DEST/resumes/"
cp "$SRC/achievements.md" "$SRC/current_resume.md" "$DEST/"

echo "corpus synced from $SRC"
ls -1 "$DEST/resumes"/*.md | wc -l | xargs echo "  resumes/*.md:"
