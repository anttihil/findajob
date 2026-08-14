"""Read the corpus off disk.

The old parser was ~400 lines of regex that split skills sections on commas and semicolons
outside parentheses. It only ever read markdown, so the PDFs sitting next to the resumes
were decoration. This module does no interpretation at all -- it hands whole documents to
the model and lets it read them, which is the point of the redesign.

**The corpus is an explicit list, not a directory scan.** `profile.corpus` in config.yaml
names the files, and a missing one is an error rather than a shrug.

Scanning `resumes/` was the original design and it is quietly dangerous: the directory also
holds tailored resumes written for *submitting* to employers, which are a different kind of
document from evidence about what someone can actually do. Six LLM-generated variants sat
there, and their inflated skill lists -- Go listed first on the strength of one side
project, plus NestJS, D3.js and CloudFormation with no backing work -- flowed straight into
the profile and therefore into every score. Nothing failed; the numbers were just wrong.

An explicit list makes adding a document a decision rather than a side effect of where a
file happens to live.

Content is hashed so `profile build` can answer "has the corpus actually changed since the
active profile was built?" without a diff of the text.
"""

import hashlib
import os

from careerradar.core.logger import get_logger
from careerradar.core.paths import REPO_ROOT

logger = get_logger()

# Big enough for a long resume, small enough that a stray PDF cannot blow up a prompt.
MAX_DOC_CHARS = 60_000


class Document:
    def __init__(self, path, kind, text):
        self.path = path
        self.kind = kind
        self.text = text
        self.sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()

    @property
    def name(self):
        return os.path.basename(self.path)

    def __repr__(self):
        return f"<Document {self.name} kind={self.kind} chars={len(self.text)}>"


def _read_markdown(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _read_pdf(path):
    from pypdf import PdfReader

    reader = PdfReader(path)
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _read(path):
    if path.lower().endswith(".pdf"):
        return _read_pdf(path)
    return _read_markdown(path)


class CorpusError(RuntimeError):
    """A named corpus document is missing or unreadable.

    Loud on purpose. A silently-skipped document changes every skill level and therefore
    every score, and the only symptom is a profile that looks plausible but is thinner
    than it should be.
    """


DEFAULT_CORPUS = [
    {"path": "achievements.md", "kind": "achievements"},
]


def corpus_spec(config=None):
    """The configured corpus list, or the default."""
    if config is None:
        from careerradar.core.config import load_config

        config = load_config()
    entries = ((config.get("profile") or {}).get("corpus")) or DEFAULT_CORPUS
    normalized = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"path": entry}
        normalized.append({
            "path": entry["path"],
            "kind": entry.get("kind", "resume"),
            "optional": bool(entry.get("optional", False)),
        })
    return normalized


def collect_documents(config=None, repo_root=REPO_ROOT):
    """Read exactly the documents `profile.corpus` names.

    Paths are relative to the repo root. `.md`, `.txt`, and `.pdf` are all read -- a PDF
    with no markdown source is a first-class corpus document, which the old regex parser
    silently ignored.
    """
    documents = []
    missing = []

    for entry in corpus_spec(config):
        path = entry["path"]
        if not os.path.isabs(path):
            path = os.path.join(repo_root, path)

        if not os.path.exists(path):
            (logger.warning("Corpus: optional document %s not found", path)
             if entry["optional"] else missing.append(entry["path"]))
            continue

        try:
            text = _read(path)
        except Exception as exc:
            raise CorpusError(f"Could not read corpus document {path}: {exc}") from exc

        if not text.strip():
            raise CorpusError(f"Corpus document {path} is empty after extraction.")

        documents.append(Document(path, entry["kind"], text[:MAX_DOC_CHARS]))

    if missing:
        raise CorpusError(
            "Missing corpus document(s): "
            + ", ".join(missing)
            + ".\nFix the paths under `profile.corpus` in config.yaml, or mark them "
              "`optional: true`."
        )
    return documents


def corpus_hash(documents):
    """One hash over the whole corpus, stable under file ordering."""
    digest = hashlib.sha256()
    for document in sorted(documents, key=lambda d: d.path):
        digest.update(document.sha256.encode("ascii"))
    return digest.hexdigest()


def render_corpus(documents):
    """Lay the corpus out for the model, one delimited block per document."""
    blocks = []
    for document in documents:
        blocks.append(
            f"<document name=\"{document.name}\" kind=\"{document.kind}\">\n"
            f"{document.text}\n"
            f"</document>"
        )
    return "\n\n".join(blocks)
