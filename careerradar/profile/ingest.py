"""Read the corpus off disk.

The old parser was ~400 lines of regex that split skills sections on commas and semicolons
outside parentheses. It only ever read markdown, so the PDFs sitting next to the resumes
were decoration. This module does no interpretation at all -- it hands whole documents to
the model and lets it read them, which is the point of the redesign.

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
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _read_pdf(path):
    from pypdf import PdfReader

    reader = PdfReader(path)
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _read(path):
    if path.lower().endswith(".pdf"):
        return _read_pdf(path)
    return _read_markdown(path)


def collect_documents(resumes_dir=None, repo_root=REPO_ROOT):
    """Gather every corpus document.

    Markdown wins over PDF for the same stem: they are two renderings of one resume, and
    the markdown is the authored source. Including both would double every skill's apparent
    evidence for no added information. A PDF with no markdown sibling *is* read -- that is
    the case the old parser silently dropped.
    """
    documents = []

    for name, kind in (
        ("current_resume.md", "current_resume"),
        ("achievements.md", "achievements"),
    ):
        path = os.path.join(repo_root, name)
        if os.path.exists(path):
            documents.append(Document(path, kind, _read(path)[:MAX_DOC_CHARS]))
        else:
            logger.warning("Corpus: %s not found at %s", name, path)

    resumes_dir = resumes_dir or os.path.join(repo_root, "resumes")
    if not os.path.isdir(resumes_dir):
        logger.warning("Corpus: no resumes directory at %s", resumes_dir)
        return documents

    entries = sorted(os.listdir(resumes_dir))
    markdown_stems = {
        os.path.splitext(e)[0] for e in entries if e.lower().endswith(".md")
    }

    for entry in entries:
        stem, ext = os.path.splitext(entry)
        ext = ext.lower()
        if ext not in (".md", ".pdf", ".txt"):
            continue
        if ext in (".pdf", ".txt") and stem in markdown_stems:
            continue
        path = os.path.join(resumes_dir, entry)
        try:
            text = _read(path)
        except Exception:
            logger.warning("Corpus: could not read %s", path, exc_info=True)
            continue
        if not text.strip():
            logger.warning("Corpus: %s is empty after extraction", path)
            continue
        documents.append(Document(path, "resume", text[:MAX_DOC_CHARS]))

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
