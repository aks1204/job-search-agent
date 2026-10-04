"""Local text embeddings (fastembed, runs on CPU, no API key, text never leaves the host)."""

from functools import lru_cache

from app.constants import EMBEDDING_DIM
from app.settings import get_settings


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding  # heavy import; only load when first needed

    s = get_settings()
    return TextEmbedding(model_name=s.embedding_model, cache_dir=s.embedding_cache_dir)


def embed(texts: list[str]) -> list[list[float]]:
    vectors = [v.tolist() for v in _model().embed(texts)]
    if vectors and len(vectors[0]) != EMBEDDING_DIM:
        raise RuntimeError(
            f"embedding model returned {len(vectors[0])} dims, schema expects {EMBEDDING_DIM}"
        )
    return vectors


def embed_one(text: str) -> list[float]:
    return embed([text])[0]
