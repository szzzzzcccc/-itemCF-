import json
from pathlib import Path
from typing import Optional

import numpy as np


ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts_runtime"
TWOTOWER_ARTIFACTS_PATH = ARTIFACT_DIR / "twotower_v1_artifacts.npz"
TWOTOWER_META_PATH = ARTIFACT_DIR / "twotower_v1_meta.json"


def save_twotower_artifacts(
    *,
    user_ids: list[int],
    movie_ids: list[int],
    user_embeddings: np.ndarray,
    movie_embeddings: np.ndarray,
    item_bias: np.ndarray,
    user_positive_counts: list[int],
    movie_positive_counts: list[int],
) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        TWOTOWER_ARTIFACTS_PATH,
        user_ids=np.asarray(user_ids, dtype=np.int32),
        movie_ids=np.asarray(movie_ids, dtype=np.int32),
        user_embeddings=np.asarray(user_embeddings, dtype=np.float32),
        movie_embeddings=np.asarray(movie_embeddings, dtype=np.float32),
        item_bias=np.asarray(item_bias, dtype=np.float32),
        user_positive_counts=np.asarray(user_positive_counts, dtype=np.int32),
        movie_positive_counts=np.asarray(movie_positive_counts, dtype=np.int32),
    )
    meta = {
        "user_count": len(user_ids),
        "movie_count": len(movie_ids),
        "embedding_dim": int(movie_embeddings.shape[1]) if movie_embeddings.ndim == 2 and movie_embeddings.size > 0 else 0,
    }
    TWOTOWER_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def load_twotower_artifacts() -> Optional[dict]:
    if not TWOTOWER_ARTIFACTS_PATH.exists():
        return None
    with np.load(TWOTOWER_ARTIFACTS_PATH, allow_pickle=False) as data:
        return {
            "user_ids": data["user_ids"].astype(np.int32, copy=False),
            "movie_ids": data["movie_ids"].astype(np.int32, copy=False),
            "user_embeddings": data["user_embeddings"].astype(np.float32, copy=False),
            "movie_embeddings": data["movie_embeddings"].astype(np.float32, copy=False),
            "item_bias": data["item_bias"].astype(np.float32, copy=False),
            "user_positive_counts": data["user_positive_counts"].astype(np.int32, copy=False),
            "movie_positive_counts": data["movie_positive_counts"].astype(np.int32, copy=False),
        }
