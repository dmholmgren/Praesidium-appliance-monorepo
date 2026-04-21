"""
Embedding generation service — generates document embedding vectors.

Uses sentence-transformers running locally on PROC-01.
No data leaves the network for embedding generation.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy-loaded model singleton
_model = None
_MODEL_NAME = os.environ.get(
    "EDISCOVERY_EMBEDDING_MODEL",
    "all-MiniLM-L6-v2",
)
_MAX_TEXT_LENGTH = 8192  # characters to embed (model context limit)


def _get_model():
    """Lazy-load the sentence transformer model."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
        logger.info("Loaded embedding model: %s", _MODEL_NAME)
    return _model


def generate_embedding(text: str) -> Optional[list[float]]:
    """
    Generate an embedding vector for a document text.
    Returns list of floats or None on failure.
    """
    if not text or not text.strip():
        return None

    try:
        model = _get_model()
        # Truncate to model context limit
        truncated = text[:_MAX_TEXT_LENGTH]
        embedding = model.encode(truncated, convert_to_numpy=True)
        return embedding.tolist()
    except Exception as e:
        logger.error("Embedding generation failed: %s", str(e))
        return None


def generate_embeddings_batch(texts: list[str]) -> list[Optional[list[float]]]:
    """Generate embeddings for a batch of texts — more efficient than one-at-a-time."""
    if not texts:
        return []

    try:
        model = _get_model()
        truncated = [t[:_MAX_TEXT_LENGTH] if t else "" for t in texts]
        embeddings = model.encode(truncated, convert_to_numpy=True, batch_size=32)
        return [e.tolist() if texts[i] else None for i, e in enumerate(embeddings)]
    except Exception as e:
        logger.error("Batch embedding generation failed: %s", str(e))
        return [None] * len(texts)
