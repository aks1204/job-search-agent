"""The candidate side of scoring: resume text (from config/resume.pdf) and the profile vector.

Temporary home until Module 5's resume flow (upload → parse → confirm). The resume is read as plain
text only — never edited, never tailored (spec §5.4).
"""

import hashlib
import math
from functools import lru_cache
from pathlib import Path

from app.config_loader import get_config
from app.config_schemas import OwnerStub
from app.core.embeddings import embed
from app.settings import get_settings

CHUNK = 1200  # characters per embedding chunk (bge-small reads ~512 tokens)


def resume_path() -> Path:
    return get_settings().config_dir / "resume.pdf"


@lru_cache(maxsize=4)
def _read_pdf(path: str, mtime: float, size: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(path)
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    return " ".join(text.split())


def resume_text() -> str:
    path = resume_path()
    if not path.is_file():
        return ""
    stat = path.stat()
    return _read_pdf(str(path), stat.st_mtime, stat.st_size)


def profile_summary(owner: OwnerStub) -> str:
    fam = owner.role_families
    return " | ".join(filter(None, [
        f"Target roles: {', '.join(owner.target_roles)}",
        f"Progression: {', '.join(fam.progression)}" if fam.progression else "",
        f"Skills: {', '.join(owner.skills.must_have + owner.skills.nice_to_have)}",
        f"Field: {owner.career_field}",
        f"Current: {owner.current_role.title} at {owner.current_role.company}",
    ]))


def similarity_version() -> str:
    """Changes when the resume file or the profile summary changes (→ similarities recomputed)."""
    owner = get_config().owner
    return hashlib.sha256((resume_text() + (profile_summary(owner) if owner else "")).encode()).hexdigest()[:32]


def _normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


@lru_cache(maxsize=2)
def _profile_vector(version: str, summary: str, resume: str) -> tuple[float, ...]:
    chunks = [summary, summary] + [resume[i:i + CHUNK] for i in range(0, min(len(resume), CHUNK * 6), CHUNK)]
    vectors = embed([c for c in chunks if c.strip()])
    mean = [sum(col) / len(vectors) for col in zip(*vectors)]
    return tuple(_normalise(mean))


def profile_vector() -> tuple[float, ...]:
    """Resume + direction profile as one vector (profile summary weighted double)."""
    owner = get_config().owner
    summary = profile_summary(owner) if owner else ""
    return _profile_vector(similarity_version(), summary, resume_text())


def job_text(job, read: dict | None) -> str:
    one_liner = (read or {}).get("one_liner", "")
    family = (read or {}).get("role_family", "")
    return f"{job.title}. {family}. {one_liner}. {(job.description or '')[:CHUNK]}"


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))  # both normalised
