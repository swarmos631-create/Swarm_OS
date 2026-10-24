"""Local-first document retrieval (BM25) with access control and provenance.

Retrieved passages are returned as quoted, attributed data. They are never treated as instructions: no
component executes or follows text found inside documents.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from swarmos.models import Document, DocumentChunk

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set("a an and are as at be by for from has have in is it of on or that the this to was were will with not no can may "
            "should shall must all any each per into than then their there these those".split())
MAX_DOC_CHARS = 400_000


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


def chunk_markdown(text: str, max_chars: int = 1200) -> list[tuple[str | None, str]]:
    """Split on headings, then on paragraphs, keeping chunks under max_chars."""
    chunks: list[tuple[str | None, str]] = []
    heading: str | None = None
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            for i in range(0, len(body), max_chars):
                chunks.append((heading, body[i : i + max_chars]))
        buf.clear()

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            heading = line.lstrip("#").strip()[:240] or None
        else:
            buf.append(line)
            if sum(len(b) for b in buf) > max_chars:
                flush()
    flush()
    return chunks


def ingest(db: Session, *, title: str, text: str, source: str, classification: str, license: str,
           city_id: str | None, uploaded_by: str | None, is_synthetic: bool = False) -> Document:
    if classification not in ("public", "internal", "restricted"):
        raise ValueError("classification must be public, internal or restricted")
    if len(text) > MAX_DOC_CHARS:
        raise ValueError(f"Document exceeds {MAX_DOC_CHARS} characters")
    digest = hashlib.sha256(text.encode()).hexdigest()
    existing = db.scalar(select(Document).where(Document.sha256 == digest))
    if existing:
        return existing
    doc = Document(title=title[:240], source=source[:300], classification=classification, license=license[:200],
                   sha256=digest, city_id=city_id, uploaded_by=uploaded_by, is_synthetic=is_synthetic)
    for i, (heading, body) in enumerate(chunk_markdown(text)):
        doc.chunks.append(DocumentChunk(ordinal=i, heading=heading, text=body))
    db.add(doc)
    db.flush()
    return doc


@dataclass
class Hit:
    score: float
    chunk: DocumentChunk
    document: Document

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "document_id": self.document.id,
            "title": self.document.title,
            "heading": self.chunk.heading,
            "passage": self.chunk.text[:700],
            "source": self.document.source,
            "classification": self.document.classification,
            "license": self.document.license,
            "synthetic": self.document.is_synthetic,
            "content_role": "retrieved_data_not_instructions",
        }


def search(db: Session, query: str, *, city_id: str, clearance: set[str], k: int = 5) -> list[Hit]:
    q_terms = tokenize(query)
    if not q_terms:
        return []
    rows = db.execute(
        select(DocumentChunk, Document)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.classification.in_(clearance), or_(Document.city_id.is_(None), Document.city_id == city_id))
    ).all()
    if not rows:
        return []
    docs_tokens = [tokenize(f"{c.heading or ''} {c.text}") for c, _ in rows]
    n = len(docs_tokens)
    avgdl = sum(len(t) for t in docs_tokens) / n
    df: Counter[str] = Counter()
    for toks in docs_tokens:
        df.update(set(toks))
    k1, b = 1.5, 0.75
    hits = []
    for (chunk, doc), toks in zip(rows, docs_tokens, strict=True):
        tf = Counter(toks)
        score = 0.0
        for term in set(q_terms):
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * len(toks) / avgdl))
        if score > 0:
            hits.append(Hit(score, chunk, doc))
    hits.sort(key=lambda h: -h.score)
    return hits[:k]
