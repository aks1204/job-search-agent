"""Memory read/write with a semantic dedupe gate (spec §5.3).

- Reads are by meaning: `search()` returns the top-k most similar entries.
- Writes go through `remember()`: a near-duplicate of an existing active entry
  updates it instead of forking. Precedents are never overwritten — the old one
  is marked superseded and linked to the new one, keeping history.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.embeddings import embed_one
from app.models.memory import MEMORY_KINDS, MemoryEntry

# Cosine distance (0 = identical). 0.08 ≈ 0.92 similarity: "same meaning, reworded".
DEDUPE_MAX_DISTANCE = 0.08


def find_similar(
    session: Session,
    kind: str,
    vector: list[float],
    k: int = 5,
    max_distance: float | None = None,
    module: str | None = None,
) -> list[tuple[MemoryEntry, float]]:
    distance = MemoryEntry.embedding.cosine_distance(vector)
    stmt = (
        select(MemoryEntry, distance.label("distance"))
        .where(
            MemoryEntry.kind == kind,
            MemoryEntry.status == "active",
            MemoryEntry.embedding.is_not(None),
        )
        .order_by(distance)
        .limit(k)
    )
    if module is not None:  # only this module's memories (company decisions and role decisions stay apart)
        stmt = stmt.where(MemoryEntry.context["module"].astext == module)
    if max_distance is not None:
        stmt = stmt.where(distance <= max_distance)
    return [(row[0], float(row[1])) for row in session.execute(stmt).all()]


def search(session: Session, kind: str, query: str, k: int = 5, *, module: str | None = None,
           mark_used: bool = True) -> list[tuple[MemoryEntry, float]]:
    """Top-k entries by meaning, as (entry, similarity). Marks them as used (unless mark_used=False)."""
    results = find_similar(session, kind, embed_one(query), k=k, module=module)
    if not mark_used:
        return [(entry, 1.0 - dist) for entry, dist in results]
    now = datetime.now(timezone.utc)
    for entry, _ in results:
        entry.usage_count += 1
        entry.last_used_at = now
    return [(entry, 1.0 - dist) for entry, dist in results]


def remember(
    session: Session,
    kind: str,
    meaning: str,
    content: dict,
    *,
    context: dict | None = None,
    provenance: dict | None = None,
    evergreen: bool = False,
    expires_at: datetime | None = None,
    dedupe: bool = True,
) -> MemoryEntry:
    """dedupe=False: the caller has already matched by an exact key (question, cluster, family), where
    similar-sounding text must NOT be merged."""
    if kind not in MEMORY_KINDS:
        raise ValueError(f"unknown memory kind {kind!r}")

    vector = embed_one(meaning)
    match = find_similar(session, kind, vector, k=1, max_distance=DEDUPE_MAX_DISTANCE) if dedupe else []

    if match and kind != "precedent":
        existing = match[0][0]
        existing.meaning = meaning
        existing.embedding = vector
        existing.content = content
        existing.context = context or existing.context
        existing.provenance = provenance or existing.provenance
        existing.evergreen = evergreen
        existing.expires_at = expires_at
        return existing

    entry = MemoryEntry(
        kind=kind,
        meaning=meaning,
        embedding=vector,
        content=content,
        context=context or {},
        provenance=provenance or {},
        evergreen=evergreen,
        expires_at=expires_at,
    )
    session.add(entry)
    session.flush()

    if match:  # precedent: supersede, keep history
        old = match[0][0]
        old.status = "superseded"
        old.superseded_by_id = entry.id

    return entry
