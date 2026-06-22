"""
Optional local semantic embeddings — static, CPU, no torch/GPU/cloud.

Uses model2vec (potion static embeddings): numpy-only, ~30 MB, sub-millisecond
encode. Strictly optional: if model2vec isn't installed, embeddings are disabled
and retrieval falls back to FTS + graph. This keeps the zero-config/local/minimal
identity intact while offering real semantic recall when the user opts in.

Enable:  pip install model2vec   (or: pip install "memory-bridge[semantic]")
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Retrieval-tuned static embedding model (numpy-only at inference).
MODEL_NAME = "minishlab/potion-retrieval-32M"

_model = None
_loaded = False


def _load():
    global _model, _loaded
    if _loaded:
        return _model
    _loaded = True
    try:
        from model2vec import StaticModel  # type: ignore
        _model = StaticModel.from_pretrained(MODEL_NAME)
        logger.info("semantic embeddings enabled (%s)", MODEL_NAME)
    except Exception as e:  # not installed, or model fetch failed → disabled
        logger.info("semantic embeddings unavailable (%s); FTS + graph only", e)
        _model = None
    return _model


def available() -> bool:
    """True if the optional embedding backend is usable."""
    return _load() is not None


def embed(texts: list[str]) -> Optional[list[list[float]]]:
    """Embed a batch of texts to float32 vectors, or None if disabled."""
    model = _load()
    if model is None or not texts:
        return None
    try:
        import numpy as np
        vecs = np.asarray(model.encode(list(texts)), dtype="float32")
        return [v.tolist() for v in vecs]
    except Exception as e:
        logger.warning("embedding failed: %s", e)
        return None


def embed_one(text: str) -> Optional[list[float]]:
    out = embed([text])
    return out[0] if out else None
