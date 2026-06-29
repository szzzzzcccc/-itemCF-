import hashlib
import json
import os
import random
import secrets
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

try:
    import faiss  # type: ignore
except ImportError:
    faiss = None

try:
    from .lgb_ranker_utils import build_lgb_candidate_rows, build_user_profile
    from .twotower_artifacts import load_twotower_artifacts
except ImportError:
    from lgb_ranker_utils import build_lgb_candidate_rows, build_user_profile
    from twotower_artifacts import load_twotower_artifacts


DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "movie_rec")
DB_USER = os.getenv("DB_USER", "movie_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "movie_pass")
DB_DSN = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"

SESSIONS: dict[str, dict] = {}
TWOTOWER_MOVIE_CACHE: Optional[dict] = None
LGB_RANKER_CACHE = None
_APP_DIR = Path(__file__).resolve().parent
LGB_RANKER_MODEL_CANDIDATES = [
    _APP_DIR / "artifacts_runtime" / "lgb_ranker.joblib",
    _APP_DIR / "artifacts" / "lgb_ranker.joblib",
]
INTERNAL_JOB_DIR = _APP_DIR / "runtime_jobs"
INTERNAL_JOB_SCRIPTS = {
    "process_incremental_rating_events": "process_incremental_rating_events.py",
    "build_sparse_recalls": "build_recalls_sparse.py",
    "train_twotower": "build_twotower.py",
    "build_lgb_training_samples": "build_lgb_training_samples.py",
    "train_lgb_ranker": "build_lgb_ranker.py",
}
USER_EXPOSURE_WINDOWS: dict[int, list[int]] = {}
USER_LAST_DELIVERED_IDS: dict[int, list[int]] = {}
USER_NEXT_BATCH_COUNTS: dict[int, int] = {}
RECALL_CHANNEL_BASE_LIMITS = {
    "popular": 30,
    "genre": 60,
    "long_tail": 20,
    "itemcf": 80,
    "twotower": 80,
}
CHANNEL_LABELS = {
    "popular": "热门召回",
    "genre": "类型偏好召回",
    "long_tail": "长尾探索召回",
    "itemcf": "ItemCF召回",
    "twotower": "双塔召回",
}
TWOTOWER_FAISS_SEARCH_MULTIPLIER = int(os.getenv("TWOTOWER_FAISS_SEARCH_MULTIPLIER", "4"))
TWOTOWER_FAISS_MIN_SEARCH = int(os.getenv("TWOTOWER_FAISS_MIN_SEARCH", "200"))
TWOTOWER_FAISS_MAX_SEARCH = int(os.getenv("TWOTOWER_FAISS_MAX_SEARCH", "1000"))
TWOTOWER_FAISS_HNSW_M = int(os.getenv("TWOTOWER_FAISS_HNSW_M", "32"))
POPULAR_RECALL_POOL_SIZE = int(os.getenv("POPULAR_RECALL_POOL_SIZE", "200"))
EXPOSURE_WINDOW_SIZE = int(os.getenv("EXPOSURE_WINDOW_SIZE", "50"))
PRE_RANK_LIMIT = int(os.getenv("PRE_RANK_LIMIT", "150"))
LONG_TAIL_MIN_VOTE_AVERAGE = float(os.getenv("LONG_TAIL_MIN_VOTE_AVERAGE", "6.5"))
LONG_TAIL_MIN_VOTE_COUNT = int(os.getenv("LONG_TAIL_MIN_VOTE_COUNT", "100"))


app = FastAPI(title="Movie Rec API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5174", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LoginRequest(BaseModel):
    username: str
    password: str


class RatingRequest(BaseModel):
    rating: float = Field(..., ge=0.5, le=5.0)


class OfflineJobStartRequest(BaseModel):
    env_overrides: dict[str, str] = Field(default_factory=dict)


def get_conn():
    return psycopg.connect(DB_DSN, row_factory=dict_row)


def ensure_internal_job_dir() -> None:
    INTERNAL_JOB_DIR.mkdir(parents=True, exist_ok=True)


def job_state_path(job_name: str) -> Path:
    return INTERNAL_JOB_DIR / f"{job_name}.json"


def read_internal_job_state(job_name: str) -> Optional[dict]:
    path = job_state_path(job_name)
    if not path.exists():
        return None
    last_error: Optional[Exception] = None
    for _attempt in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    return None


def write_internal_job_state(job_name: str, payload: dict) -> None:
    ensure_internal_job_dir()
    target_path = job_state_path(job_name)
    temp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(target_path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tail_text(path: Path, line_limit: int = 40) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-line_limit:])


def build_internal_job_response(job_name: str, line_limit: int = 40) -> Optional[dict]:
    state = read_internal_job_state(job_name)
    if state is None:
        return None
    log_path_value = state.get("log_path")
    if log_path_value:
        log_path = Path(log_path_value)
        state["log_tail"] = tail_text(log_path, line_limit=line_limit)
    else:
        state["log_tail"] = ""
    return state


def run_internal_job(job_name: str, run_id: str, script_name: str, env_overrides: dict[str, str]) -> None:
    global TWOTOWER_MOVIE_CACHE, LGB_RANKER_CACHE
    log_path = INTERNAL_JOB_DIR / f"{job_name}_{run_id}.log"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_APP_DIR)
    env.update({key: str(value) for key, value in env_overrides.items()})
    cmd = [sys.executable, "-u", script_name]

    state = read_internal_job_state(job_name) or {}
    state.update(
        {
            "job_name": job_name,
            "run_id": run_id,
            "script_name": script_name,
            "status": "starting",
            "started_at": now_iso(),
            "finished_at": None,
            "return_code": None,
            "pid": None,
            "log_path": str(log_path),
            "env_overrides": env_overrides,
            "command": " ".join(cmd),
        }
    )
    write_internal_job_state(job_name, state)

    try:
        ensure_internal_job_dir()
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(f"[job] run_id={run_id} script={script_name}\n")
            log_fp.write(f"[job] env_overrides={json.dumps(env_overrides, ensure_ascii=False)}\n")
            log_fp.flush()
            process = subprocess.Popen(
                cmd,
                cwd=str(_APP_DIR),
                env=env,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
            )
            state["status"] = "running"
            state["pid"] = process.pid
            write_internal_job_state(job_name, state)
            return_code = process.wait()
            state["return_code"] = int(return_code)
            state["finished_at"] = now_iso()
            state["status"] = "success" if return_code == 0 else "failed"
            if return_code == 0 and job_name == "train_twotower":
                TWOTOWER_MOVIE_CACHE = None
            if return_code == 0 and job_name == "train_lgb_ranker":
                LGB_RANKER_CACHE = None
            write_internal_job_state(job_name, state)
    except Exception as exc:
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(f"[job] exception={exc}\n")
            log_fp.write(traceback.format_exc())
            log_fp.flush()
        state["status"] = "failed"
        state["finished_at"] = now_iso()
        state["return_code"] = -1
        state["error"] = str(exc)
        write_internal_job_state(job_name, state)


def log_rating_event(
    cur: psycopg.Cursor,
    *,
    user_id: int,
    movie_id: int,
    event_type: str,
    previous_rating: Optional[float],
    new_rating: Optional[float],
    event_ts: int,
) -> None:
    cur.execute(
        """
        INSERT INTO rating_events (
            user_id,
            movie_id,
            event_type,
            previous_rating,
            new_rating,
            event_ts
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (user_id, movie_id, event_type, previous_rating, new_rating, event_ts),
    )


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def parse_genres(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [genre for genre in value.split("|") if genre]


def parse_embedding_json(embedding_json: str) -> np.ndarray:
    return np.asarray(json.loads(embedding_json), dtype=np.float32)


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-8:
        return vector
    return (vector / norm).astype(np.float32)


def l2_normalize_matrix(matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0:
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms <= 1e-8, 1.0, norms)
    return (matrix / norms).astype(np.float32)


def build_user_payload(row: dict) -> dict:
    return {
        "app_user_id": row["app_user_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "movielens_user_id": row["movielens_user_id"],
    }


def build_movie_payload(row: dict) -> dict:
    release_date = row.get("release_date")
    return {
        "movie_id": row["movie_id"],
        "title": row.get("tmdb_title") or row.get("original_title") or row.get("movielens_title"),
        "original_title": row.get("original_title") or row.get("movielens_title"),
        "overview": row.get("overview"),
        "poster_url": row.get("poster_url"),
        "release_date": release_date.isoformat() if release_date else None,
        "release_year": release_date.year if release_date else None,
        "vote_average": float(row["vote_average"]) if row.get("vote_average") is not None else None,
        "vote_count": row.get("vote_count"),
        "popularity": float(row["popularity"]) if row.get("popularity") is not None else None,
        "genres": parse_genres(row.get("tmdb_genres")),
    }


def enrich_movie_payload(row: dict, **extra_fields) -> dict:
    payload = build_movie_payload(row)
    payload.update(extra_fields)
    return payload


def get_current_user(authorization: Optional[str] = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization.replace("Bearer ", "", 1).strip()
    session = SESSIONS.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="登录状态已失效")
    return session


def ensure_half_step(rating: float) -> None:
    doubled = rating * 2
    if abs(doubled - round(doubled)) > 1e-8:
        raise HTTPException(status_code=400, detail="评分必须是 0.5 的倍数")


def parse_exclude_ids(exclude_ids: Optional[str]) -> set[int]:
    if not exclude_ids:
        return set()
    result: set[int] = set()
    for raw_part in exclude_ids.split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def filter_items_by_excluded(items: list[dict], excluded_ids: set[int], limit: Optional[int] = None) -> list[dict]:
    if not excluded_ids:
        filtered = [dict(item) for item in items]
    else:
        filtered = [dict(item) for item in items if int(item["movie_id"]) not in excluded_ids]
    if limit is None:
        return filtered
    return filtered[:limit]


def compute_personalized_recall_extra(next_batch_count: int) -> int:
    if next_batch_count <= 0:
        return 0
    if next_batch_count <= 2:
        return next_batch_count * 20
    return 40 + (next_batch_count - 2) * 10


def compute_recall_channel_limits(next_batch_count: int) -> dict[str, int]:
    personalized_extra = compute_personalized_recall_extra(next_batch_count)
    return {
        "popular": RECALL_CHANNEL_BASE_LIMITS["popular"],
        "genre": min(RECALL_CHANNEL_BASE_LIMITS["genre"] + personalized_extra, 180),
        "long_tail": RECALL_CHANNEL_BASE_LIMITS["long_tail"],
        "itemcf": min(RECALL_CHANNEL_BASE_LIMITS["itemcf"] + personalized_extra, 240),
        "twotower": min(RECALL_CHANNEL_BASE_LIMITS["twotower"] + personalized_extra, 240),
    }


def compute_rank_candidate_limit(channel_limits: dict[str, int]) -> int:
    return min(sum(channel_limits.values()), PRE_RANK_LIMIT)


def reset_user_exposure_state(user_id: int) -> None:
    USER_EXPOSURE_WINDOWS.pop(user_id, None)
    USER_LAST_DELIVERED_IDS.pop(user_id, None)
    USER_NEXT_BATCH_COUNTS.pop(user_id, None)


def get_user_exposure_window(user_id: int) -> list[int]:
    return list(USER_EXPOSURE_WINDOWS.get(user_id, []))


def advance_user_exposure_window(user_id: int, window_size: int = EXPOSURE_WINDOW_SIZE) -> list[int]:
    current_window = list(USER_EXPOSURE_WINDOWS.get(user_id, []))
    last_delivered_ids = USER_LAST_DELIVERED_IDS.get(user_id, [])
    if last_delivered_ids:
        current_window.extend(int(movie_id) for movie_id in last_delivered_ids)
    if len(current_window) > window_size:
        current_window = current_window[-window_size:]
    USER_EXPOSURE_WINDOWS[user_id] = current_window
    return list(current_window)


def save_user_last_delivered_ids(user_id: int, items: list[dict]) -> None:
    USER_LAST_DELIVERED_IDS[user_id] = [int(item["movie_id"]) for item in items]


def get_user_next_batch_count(user_id: int) -> int:
    return int(USER_NEXT_BATCH_COUNTS.get(user_id, 0))


def increment_user_next_batch_count(user_id: int) -> int:
    next_count = get_user_next_batch_count(user_id) + 1
    USER_NEXT_BATCH_COUNTS[user_id] = next_count
    return next_count


def clamp_score(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def quality_score(vote_average: Optional[float]) -> float:
    if vote_average is None:
        return 0.0
    return clamp_score((float(vote_average) - 6.0) / 2.5)


def stability_score(vote_count: Optional[int]) -> float:
    if not vote_count or int(vote_count) <= 0:
        return 0.0
    return clamp_score(float(np.log1p(int(vote_count)) / np.log1p(3000)))


def load_user_seen_movie_ids(user_id: int) -> set[int]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT movie_id
                FROM ratings
                WHERE user_id = %s
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    return {int(row["movie_id"]) for row in rows}


def build_genre_preference_profile(history_rows: list[dict]) -> dict[str, float]:
    if not history_rows:
        return {}

    genre_scores: dict[str, float] = {}
    total_rows = len(history_rows)
    for index, row in enumerate(history_rows):
        genres = row.get("genres") or []
        if not genres:
            continue
        rating_weight = max(float(row.get("rating") or 4.0) - 3.0, 0.5)
        recency_weight = 0.7 + 0.3 * ((index + 1) / total_rows)
        row_weight = rating_weight * recency_weight
        for genre in genres:
            genre_scores[genre] = genre_scores.get(genre, 0.0) + row_weight

    total_score = sum(genre_scores.values())
    if total_score <= 1e-8:
        return {}
    return {
        genre: score / total_score
        for genre, score in sorted(genre_scores.items(), key=lambda item: item[1], reverse=True)
    }


def compute_genre_match_score(movie_genres: list[str], genre_profile: dict[str, float]) -> float:
    if not movie_genres or not genre_profile:
        return 0.0
    matched_weights = [genre_profile.get(genre, 0.0) for genre in movie_genres if genre_profile.get(genre, 0.0) > 0.0]
    if not matched_weights:
        return 0.0
    matched_weight_sum = sum(matched_weights)
    matched_count = len(matched_weights)
    return clamp_score(0.7 * matched_weight_sum + 0.3 * (min(matched_count, 3) / 3.0))


def load_popular_pool_movie_ids(pool_limit: int = POPULAR_RECALL_POOL_SIZE) -> set[int]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT movie_id
                FROM recall_popular_movies
                ORDER BY rank_no
                LIMIT %s
                """,
                (pool_limit,),
            )
            rows = cur.fetchall()
    return {int(row["movie_id"]) for row in rows}


def load_movies_by_genre_candidates(
    candidate_genres: list[str],
    seen_movie_ids: set[int],
    min_vote_average: Optional[float] = None,
    min_vote_count: Optional[int] = None,
    excluded_movie_ids: Optional[set[int]] = None,
) -> list[dict]:
    if not candidate_genres:
        return []

    filtered_seen_ids = sorted(int(movie_id) for movie_id in seen_movie_ids)
    filtered_excluded_ids = sorted(int(movie_id) for movie_id in (excluded_movie_ids or set()))

    where_clauses = [
        "m.poster_url IS NOT NULL",
        "COALESCE(m.tmdb_genres, '') <> ''",
        "string_to_array(m.tmdb_genres, '|') && %s::text[]",
    ]
    params: list[object] = [candidate_genres]

    if filtered_seen_ids:
        where_clauses.append("NOT (m.movie_id = ANY(%s::int[]))")
        params.append(filtered_seen_ids)
    if filtered_excluded_ids:
        where_clauses.append("NOT (m.movie_id = ANY(%s::int[]))")
        params.append(filtered_excluded_ids)
    if min_vote_average is not None:
        where_clauses.append("COALESCE(m.vote_average, 0) >= %s")
        params.append(float(min_vote_average))
    if min_vote_count is not None:
        where_clauses.append("COALESCE(m.vote_count, 0) >= %s")
        params.append(int(min_vote_count))

    query = f"""
        SELECT
            m.movie_id,
            m.movielens_title,
            m.tmdb_title,
            m.original_title,
            m.overview,
            m.poster_url,
            m.release_date,
            m.vote_average,
            m.vote_count,
            m.popularity,
            m.tmdb_genres
        FROM movies m
        WHERE {' AND '.join(where_clauses)}
    """

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()


def get_genre_recall_recommendations(
    user_id: int,
    limit: int,
    history_rows: list[dict],
    seen_movie_ids: set[int],
) -> tuple[list[dict], dict[str, float]]:
    genre_profile = build_genre_preference_profile(history_rows)
    if not genre_profile:
        return [], {}

    candidate_genres = list(genre_profile.keys())[:4]
    rows = load_movies_by_genre_candidates(candidate_genres, seen_movie_ids)
    scored_items: list[dict] = []
    for row in rows:
        movie_genres = parse_genres(row.get("tmdb_genres"))
        genre_match = compute_genre_match_score(movie_genres, genre_profile)
        if genre_match <= 0.0:
            continue
        movie_quality_score = quality_score(row.get("vote_average"))
        movie_stability_score = stability_score(row.get("vote_count"))
        recall_score = 0.60 * genre_match + 0.25 * movie_quality_score + 0.15 * movie_stability_score
        payload = enrich_movie_payload(
            row,
            recall_score=round(float(recall_score), 6),
            genre_match_score=round(float(genre_match), 6),
            quality_score=round(float(movie_quality_score), 6),
            stability_score=round(float(movie_stability_score), 6),
            channel="genre",
            reason="类型偏好召回：命中了你高分历史里最偏好的电影类型",
        )
        scored_items.append(payload)

    scored_items.sort(
        key=lambda item: (
            item["recall_score"],
            item.get("genre_match_score", 0.0),
            item.get("vote_average") or 0.0,
            item["movie_id"],
        ),
        reverse=True,
    )
    return scored_items[:limit], genre_profile


def get_long_tail_recall_recommendations(
    limit: int,
    genre_profile: dict[str, float],
    seen_movie_ids: set[int],
    popular_pool_movie_ids: set[int],
) -> list[dict]:
    if not genre_profile:
        return []

    candidate_genres = list(genre_profile.keys())[:3]
    rows = load_movies_by_genre_candidates(
        candidate_genres,
        seen_movie_ids,
        min_vote_average=LONG_TAIL_MIN_VOTE_AVERAGE,
        min_vote_count=LONG_TAIL_MIN_VOTE_COUNT,
        excluded_movie_ids=popular_pool_movie_ids,
    )
    if not rows:
        return []

    rows_sorted_by_popularity = sorted(rows, key=lambda row: (float(row.get("popularity") or 0.0), row["movie_id"]), reverse=True)
    total_candidates = len(rows_sorted_by_popularity)
    popularity_rank_map = {
        int(row["movie_id"]): rank
        for rank, row in enumerate(rows_sorted_by_popularity, start=1)
    }

    scored_items: list[dict] = []
    for row in rows:
        movie_genres = parse_genres(row.get("tmdb_genres"))
        genre_match = compute_genre_match_score(movie_genres, genre_profile)
        if genre_match <= 0.0:
            continue
        movie_quality_score = quality_score(row.get("vote_average"))
        if total_candidates <= 1:
            long_tail_bonus = 1.0
        else:
            popularity_rank = popularity_rank_map[int(row["movie_id"])]
            long_tail_bonus = (popularity_rank - 1) / (total_candidates - 1)
        recall_score = 0.45 * genre_match + 0.25 * movie_quality_score + 0.30 * long_tail_bonus
        payload = enrich_movie_payload(
            row,
            recall_score=round(float(recall_score), 6),
            genre_match_score=round(float(genre_match), 6),
            quality_score=round(float(movie_quality_score), 6),
            long_tail_bonus=round(float(long_tail_bonus), 6),
            channel="long_tail",
            reason="长尾探索召回：在你偏好的类型里优先挑选相对不那么热门但质量过关的电影",
        )
        scored_items.append(payload)

    scored_items.sort(
        key=lambda item: (
            item["recall_score"],
            item.get("long_tail_bonus", 0.0),
            item.get("vote_average") or 0.0,
            item["movie_id"],
        ),
        reverse=True,
    )
    return scored_items[:limit]


def sample_popular_rows(rows: list[dict], limit: int) -> list[dict]:
    if len(rows) <= limit:
        return rows
    sampled_rows = random.sample(rows, limit)
    sampled_rows.sort(key=lambda row: (row["rank_no"], row["movie_id"]))
    return sampled_rows


def get_popular_recommendations(limit: int) -> list[dict]:
    pool_limit = max(limit, POPULAR_RECALL_POOL_SIZE)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.movie_id,
                    p.rank_no,
                    p.recall_score,
                    p.rating_count,
                    p.avg_rating,
                    m.movielens_title,
                    m.tmdb_title,
                    m.original_title,
                    m.overview,
                    m.poster_url,
                    m.release_date,
                    m.vote_average,
                    m.vote_count,
                    m.popularity,
                    m.tmdb_genres
                FROM recall_popular_movies p
                JOIN movies m
                    ON m.movie_id = p.movie_id
                WHERE m.poster_url IS NOT NULL
                ORDER BY p.rank_no
                LIMIT %s
                """,
                (pool_limit,),
            )
            rows = cur.fetchall()

    rows = sample_popular_rows(rows, limit)

    return [
        enrich_movie_payload(
            row,
            rank_no=row["rank_no"],
            recall_score=float(row["recall_score"]),
            channel="popular",
            reason="热门召回：综合了平台热度、评分人数和平均评分",
        )
        for row in rows
    ]


def get_itemcf_recommendations(user_id: int, limit: int) -> tuple[list[dict], int]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS positive_seed_count
                FROM ratings
                WHERE user_id = %s AND rating >= 4.0
                """,
                (user_id,),
            )
            positive_seed_count = cur.fetchone()["positive_seed_count"]

            cur.execute(
                """
                WITH user_positive AS (
                    SELECT movie_id, rating
                    FROM ratings
                    WHERE user_id = %s AND rating >= 4.0
                ),
                seen_movies AS (
                    SELECT movie_id
                    FROM ratings
                    WHERE user_id = %s
                ),
                itemcf_scored AS (
                    SELECT
                        sim.target_movie_id AS movie_id,
                        SUM(sim.sim_score * up.rating) AS recall_score,
                        MAX(sim.sim_score) AS best_similarity,
                        COUNT(*)::INTEGER AS supporting_seed_count,
                        (
                            ARRAY_AGG(
                                COALESCE(seed.tmdb_title, seed.original_title, seed.movielens_title)
                                ORDER BY sim.sim_score DESC, up.rating DESC, seed.movie_id DESC
                            )
                        )[1] AS top_seed_title
                    FROM user_positive up
                    JOIN recall_itemcf_similarity sim
                        ON sim.source_movie_id = up.movie_id
                    JOIN movies seed
                        ON seed.movie_id = up.movie_id
                    LEFT JOIN seen_movies seen
                        ON seen.movie_id = sim.target_movie_id
                    WHERE seen.movie_id IS NULL
                    GROUP BY sim.target_movie_id
                )
                SELECT
                    s.movie_id,
                    s.recall_score,
                    s.best_similarity,
                    s.supporting_seed_count,
                    s.top_seed_title,
                    m.movielens_title,
                    m.tmdb_title,
                    m.original_title,
                    m.overview,
                    m.poster_url,
                    m.release_date,
                    m.vote_average,
                    m.vote_count,
                    m.popularity,
                    m.tmdb_genres
                FROM itemcf_scored s
                JOIN movies m
                    ON m.movie_id = s.movie_id
                WHERE m.poster_url IS NOT NULL
                ORDER BY s.recall_score DESC, s.best_similarity DESC, s.movie_id DESC
                LIMIT %s
                """,
                (user_id, user_id, limit),
            )
            rows = cur.fetchall()

    items = [
        enrich_movie_payload(
            row,
            recall_score=float(row["recall_score"]),
            best_similarity=float(row["best_similarity"]),
            supporting_seed_count=row["supporting_seed_count"],
            channel="itemcf",
            reason=f"ItemCF召回：与你高分看过的《{row['top_seed_title']}》相似",
        )
        for row in rows
    ]
    return items, positive_seed_count


def load_twotower_movie_cache(force_reload: bool = False) -> dict:
    global TWOTOWER_MOVIE_CACHE
    if TWOTOWER_MOVIE_CACHE is not None and not force_reload:
        return TWOTOWER_MOVIE_CACHE
    artifact_data = load_twotower_artifacts()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    e.movie_id,
                    e.positive_user_count,
                    m.movielens_title,
                    m.tmdb_title,
                    m.original_title,
                    m.overview,
                    m.poster_url,
                    m.release_date,
                    m.vote_average,
                    m.vote_count,
                    m.popularity,
                    m.tmdb_genres
                FROM recall_twotower_movie_embeddings e
                JOIN movies m
                    ON m.movie_id = e.movie_id
                WHERE m.poster_url IS NOT NULL
                ORDER BY e.movie_id
                """
            )
            rows = cur.fetchall()

    if not rows:
        TWOTOWER_MOVIE_CACHE = {
            "movie_ids": np.array([], dtype=np.int32),
            "embeddings": np.empty((0, 0), dtype=np.float32),
            "normalized_embeddings": np.empty((0, 0), dtype=np.float32),
            "biases": np.array([], dtype=np.float32),
            "movie_index": {},
            "user_index": {},
            "user_embeddings": np.empty((0, 0), dtype=np.float32),
            "user_positive_counts": np.array([], dtype=np.int32),
            "faiss_index": None,
            "faiss_enabled": False,
            "payloads": [],
        }
        return TWOTOWER_MOVIE_CACHE

    if artifact_data is not None:
        artifact_movie_index = {
            int(movie_id): idx
            for idx, movie_id in enumerate(artifact_data["movie_ids"].tolist())
        }
        rows = [row for row in rows if int(row["movie_id"]) in artifact_movie_index]
        selected_indices = np.asarray(
            [artifact_movie_index[int(row["movie_id"])] for row in rows],
            dtype=np.int32,
        )
        embeddings = artifact_data["movie_embeddings"][selected_indices].astype(np.float32, copy=False)
        biases = artifact_data["item_bias"][selected_indices].astype(np.float32, copy=False)
        movie_positive_counts = artifact_data["movie_positive_counts"][selected_indices].astype(np.int32, copy=False)
        user_ids = artifact_data["user_ids"].astype(np.int32, copy=False)
        user_embeddings = artifact_data["user_embeddings"].astype(np.float32, copy=False)
        user_positive_counts = artifact_data["user_positive_counts"].astype(np.int32, copy=False)
    else:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, embedding_json, positive_count
                    FROM recall_twotower_user_embeddings
                    ORDER BY user_id
                    """
                )
                user_rows = cur.fetchall()

                cur.execute(
                    """
                    SELECT movie_id, embedding_json, item_bias, positive_user_count
                    FROM recall_twotower_movie_embeddings
                    ORDER BY movie_id
                    """
                )
                movie_rows = cur.fetchall()

        movie_row_map = {int(row["movie_id"]): row for row in movie_rows}
        rows = [row for row in rows if int(row["movie_id"]) in movie_row_map]
        embeddings = np.vstack(
            [parse_embedding_json(movie_row_map[int(row["movie_id"])]["embedding_json"]) for row in rows]
        ).astype(np.float32)
        biases = np.asarray(
            [float(movie_row_map[int(row["movie_id"])]["item_bias"]) for row in rows],
            dtype=np.float32,
        )
        movie_positive_counts = np.asarray(
            [int(movie_row_map[int(row["movie_id"])]["positive_user_count"]) for row in rows],
            dtype=np.int32,
        )
        user_ids = np.asarray([int(row["user_id"]) for row in user_rows], dtype=np.int32)
        if user_rows:
            user_embeddings = np.vstack([parse_embedding_json(row["embedding_json"]) for row in user_rows]).astype(np.float32)
        else:
            user_embeddings = np.empty((0, embeddings.shape[1]), dtype=np.float32)
        user_positive_counts = np.asarray([int(row["positive_count"]) for row in user_rows], dtype=np.int32)

    normalized_embeddings = l2_normalize_matrix(embeddings)
    faiss_index = None
    faiss_enabled = False
    if faiss is not None and normalized_embeddings.size > 0:
        dim = int(normalized_embeddings.shape[1])
        faiss_index = faiss.IndexHNSWFlat(dim, TWOTOWER_FAISS_HNSW_M, faiss.METRIC_INNER_PRODUCT)
        faiss_index.hnsw.efConstruction = max(80, TWOTOWER_FAISS_HNSW_M * 4)
        faiss_index.hnsw.efSearch = max(64, TWOTOWER_FAISS_HNSW_M * 2)
        faiss_index.add(normalized_embeddings)
        faiss_enabled = True

    TWOTOWER_MOVIE_CACHE = {
        "movie_ids": np.asarray([row["movie_id"] for row in rows], dtype=np.int32),
        "embeddings": embeddings,
        "normalized_embeddings": normalized_embeddings,
        "biases": biases,
        "movie_index": {int(row["movie_id"]): idx for idx, row in enumerate(rows)},
        "user_index": {int(user_id): idx for idx, user_id in enumerate(user_ids.tolist())},
        "user_embeddings": user_embeddings,
        "user_positive_counts": user_positive_counts,
        "faiss_index": faiss_index,
        "faiss_enabled": faiss_enabled,
        "payloads": [
            enrich_movie_payload(
                row,
                positive_user_count=int(movie_positive_counts[idx]),
            )
            for idx, row in enumerate(rows)
        ],
    }
    return TWOTOWER_MOVIE_CACHE


def build_fallback_twotower_user_vector(
    history_rows: list[dict],
    cache: dict,
) -> tuple[Optional[np.ndarray], int]:
    positive_rows = [row for row in history_rows if float(row["rating"]) >= 4.0]
    if not positive_rows:
        return None, 0

    vectors: list[np.ndarray] = []
    weights: list[float] = []
    movie_index = cache["movie_index"]
    for row in positive_rows[:100]:
        idx = movie_index.get(int(row["movie_id"]))
        if idx is None:
            continue
        vectors.append(cache["embeddings"][idx])
        weights.append(float(row["rating"]))

    if not vectors:
        return None, 0

    weight_array = np.asarray(weights, dtype=np.float32)
    stacked = np.vstack(vectors).astype(np.float32)
    user_vector = np.average(stacked, axis=0, weights=weight_array).astype(np.float32)
    return l2_normalize(user_vector), len(vectors)


def compute_twotower_search_limit(limit: int, seen_count: int, total_movies: int) -> int:
    desired = max(limit * TWOTOWER_FAISS_SEARCH_MULTIPLIER, limit + seen_count)
    desired = max(desired, TWOTOWER_FAISS_MIN_SEARCH)
    desired = min(desired, TWOTOWER_FAISS_MAX_SEARCH, total_movies)
    return max(desired, limit)


def get_twotower_recommendations(user_id: int, limit: int) -> tuple[list[dict], int]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT movie_id, rating
                FROM ratings
                WHERE user_id = %s
                ORDER BY rating_timestamp DESC
                """,
                (user_id,),
            )
            history_rows = cur.fetchall()

    cache = load_twotower_movie_cache()
    if cache["movie_ids"].size == 0:
        return [], 0

    recall_reason = "双塔召回：根据你的历史偏好向量与电影向量匹配得到"
    user_idx = cache["user_index"].get(int(user_id))
    if user_idx is not None:
        user_vector = cache["user_embeddings"][user_idx]
        positive_count = int(cache["user_positive_counts"][user_idx])
    else:
        user_vector, positive_count = build_fallback_twotower_user_vector(history_rows, cache)
        if user_vector is None:
            return [], 0
        recall_reason = "双塔召回：基于你的高分历史临时聚合用户向量后匹配得到"

    seen_movie_ids = {row["movie_id"] for row in history_rows}
    scores = cache["embeddings"] @ user_vector + cache["biases"]
    if cache.get("faiss_enabled") and cache.get("faiss_index") is not None:
        search_limit = compute_twotower_search_limit(limit, len(seen_movie_ids), len(cache["movie_ids"]))
        normalized_user_vector = l2_normalize(user_vector).reshape(1, -1).astype(np.float32)
        _ann_scores, ann_indices = cache["faiss_index"].search(normalized_user_vector, search_limit)
        ranked_indices = [int(idx) for idx in ann_indices[0].tolist() if idx >= 0]
        if len(ranked_indices) < limit:
            full_ranked = np.argsort(scores)[::-1].tolist()
            seen_ann = set(ranked_indices)
            ranked_indices.extend(idx for idx in full_ranked if idx not in seen_ann)
    else:
        ranked_indices = np.argsort(scores)[::-1].tolist()

    items: list[dict] = []
    for idx in ranked_indices:
        movie_id = int(cache["movie_ids"][idx])
        if movie_id in seen_movie_ids:
            continue
        payload = dict(cache["payloads"][idx])
        payload.update(
            {
                "recall_score": float(scores[idx]),
                "channel": "twotower",
                "reason": recall_reason,
            }
        )
        items.append(payload)
        if len(items) >= limit:
            break

    return items, positive_count


def load_lgb_ranker_model():
    global LGB_RANKER_CACHE
    if LGB_RANKER_CACHE is not None:
        return LGB_RANKER_CACHE
    model_path = next((path for path in LGB_RANKER_MODEL_CANDIDATES if path.exists()), None)
    if model_path is None:
        return None
    LGB_RANKER_CACHE = joblib.load(model_path)
    return LGB_RANKER_CACHE


def load_user_positive_history_rows(user_id: int, limit: int = 30) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    r.movie_id,
                    r.rating,
                    r.rating_timestamp,
                    m.release_date,
                    m.vote_average,
                    m.vote_count,
                    m.popularity,
                    m.tmdb_genres
                FROM ratings r
                JOIN movies m
                    ON m.movie_id = r.movie_id
                WHERE r.user_id = %s
                  AND r.rating >= 4.0
                  AND m.poster_url IS NOT NULL
                ORDER BY r.rating_timestamp DESC, r.movie_id DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
            rows = cur.fetchall()

    history_rows = []
    for row in reversed(rows):
        release_date = row.get("release_date")
        history_rows.append(
            {
                "movie_id": row["movie_id"],
                "rating": float(row.get("rating") or 0.0),
                "rating_timestamp": row.get("rating_timestamp"),
                "release_year": release_date.year if release_date else None,
                "vote_average": float(row["vote_average"]) if row.get("vote_average") is not None else 0.0,
                "vote_count": int(row["vote_count"]) if row.get("vote_count") is not None else 0,
                "popularity": float(row["popularity"]) if row.get("popularity") is not None else 0.0,
                "genres": parse_genres(row.get("tmdb_genres")),
            }
        )
    return history_rows


def merge_recall_results(
    popular_items: list[dict],
    itemcf_items: list[dict],
    twotower_items: list[dict],
    limit: int,
) -> list[dict]:
    merged_map: dict[int, dict] = {}
    channel_specs = [
        ("twotower", twotower_items, 0.45),
        ("itemcf", itemcf_items, 0.40),
        ("popular", popular_items, 0.15),
    ]

    for channel_name, items, weight in channel_specs:
        for index, item in enumerate(items):
            movie_id = item["movie_id"]
            bonus = weight / (index + 1)
            if movie_id not in merged_map:
                payload = dict(item)
                payload["merged_score"] = bonus
                payload["source_channels"] = [channel_name]
                merged_map[movie_id] = payload
            else:
                merged_map[movie_id]["merged_score"] += bonus
                merged_map[movie_id]["source_channels"].append(channel_name)

    for payload in merged_map.values():
        channels = payload["source_channels"]
        if len(channels) >= 2:
            payload["reason"] = f"组合召回：同时命中了{'、'.join(CHANNEL_LABELS.get(channel, channel) for channel in channels)}"
            continue
        payload["reason"] = f"组合召回：来自{CHANNEL_LABELS.get(channels[0], channels[0])}"

    merged_items = sorted(
        merged_map.values(),
        key=lambda item: (item["merged_score"], item.get("recall_score", 0), item["movie_id"]),
        reverse=True,
    )

    results = []
    for rank, item in enumerate(merged_items[:limit], start=1):
        payload = dict(item)
        payload["rank_no"] = rank
        results.append(payload)
    return results


def merge_recall_results_v2(
    popular_items: list[dict],
    genre_items: list[dict],
    long_tail_items: list[dict],
    itemcf_items: list[dict],
    twotower_items: list[dict],
    limit: int,
) -> list[dict]:
    merged_map: dict[int, dict] = {}
    channel_specs = [
        ("twotower", twotower_items, 0.30),
        ("itemcf", itemcf_items, 0.27),
        ("genre", genre_items, 0.18),
        ("long_tail", long_tail_items, 0.10),
        ("popular", popular_items, 0.15),
    ]

    for channel_name, items, weight in channel_specs:
        for index, item in enumerate(items):
            movie_id = item["movie_id"]
            bonus = weight / (index + 1)
            if movie_id not in merged_map:
                payload = dict(item)
                payload["merged_score"] = bonus
                payload["source_channels"] = [channel_name]
                merged_map[movie_id] = payload
            else:
                merged_map[movie_id]["merged_score"] += bonus
                merged_map[movie_id]["source_channels"].append(channel_name)

    for payload in merged_map.values():
        channels = payload["source_channels"]
        if len(channels) >= 3:
            payload["reason"] = f"组合召回：同时命中了{'、'.join(CHANNEL_LABELS.get(channel, channel) for channel in channels)}"
        elif "twotower" in channels and "itemcf" in channels:
            payload["reason"] = "组合召回：同时来自双塔召回和 ItemCF 召回"
        elif "twotower" in channels and "popular" in channels:
            payload["reason"] = "组合召回：同时来自双塔召回和热门召回"
        elif "itemcf" in channels and "popular" in channels:
            payload["reason"] = "组合召回：同时来自 ItemCF 召回和热门召回"

    merged_items = sorted(
        merged_map.values(),
        key=lambda item: (item["merged_score"], item.get("recall_score", 0), item["movie_id"]),
        reverse=True,
    )

    results = []
    for rank, item in enumerate(merged_items[:limit], start=1):
        payload = dict(item)
        payload["rank_no"] = rank
        results.append(payload)
    return results


def rank_candidates_with_lgb(
    popular_items: list[dict],
    genre_items: list[dict],
    long_tail_items: list[dict],
    itemcf_items: list[dict],
    twotower_items: list[dict],
    merged_candidates: list[dict],
    user_history_rows: list[dict],
) -> tuple[list[dict], dict]:
    model = load_lgb_ranker_model()
    if model is None:
        return merged_candidates, {"model_loaded": False}

    user_profile = build_user_profile(user_history_rows)
    candidate_rows = build_lgb_candidate_rows(
        popular_items,
        genre_items,
        long_tail_items,
        itemcf_items,
        twotower_items,
        merged_candidates,
        user_profile,
    )
    if not candidate_rows:
        return [], {"model_loaded": True, "candidate_count": 0}

    x_rows = np.asarray([row["features"] for row in candidate_rows], dtype=np.float32)
    pred_scores = model.predict(x_rows)
    ranked = []
    for row, pred_score in zip(candidate_rows, pred_scores):
        payload = dict(row["payload"])
        payload["rank_score"] = round(float(pred_score), 6)
        ranked.append(payload)
    ranked.sort(key=lambda item: (item["rank_score"], item.get("merged_score", 0.0), item["movie_id"]), reverse=True)
    for idx, item in enumerate(ranked, start=1):
        item["rank_no"] = idx
        item["channel"] = "lgb_ranker"
        item["reason"] = "LightGBM 精排：综合召回分数、历史偏好和电影属性打分"
    return ranked, {"model_loaded": True, "candidate_count": len(ranked)}


def choose_mmr_window_size(limit: int) -> int:
    if limit <= 10:
        return 3
    if limit <= 20:
        return 4
    return 5


def year_similarity(year_a: Optional[int], year_b: Optional[int]) -> float:
    if year_a is None or year_b is None:
        return 0.0
    return max(0.0, 1.0 - abs(year_a - year_b) / 10.0)


def movie_similarity(movie_a: dict, movie_b: dict) -> float:
    genres_a = set(movie_a.get("genres") or [])
    genres_b = set(movie_b.get("genres") or [])
    if not genres_a and not genres_b:
        genre_overlap = 0.0
    else:
        union = genres_a | genres_b
        genre_overlap = len(genres_a & genres_b) / len(union) if union else 0.0
    year_sim = year_similarity(movie_a.get("release_year"), movie_b.get("release_year"))
    return 0.7 * genre_overlap + 0.3 * year_sim


def rerank_with_sliding_window_mmr(items: list[dict], final_limit: int) -> tuple[list[dict], dict]:
    if not items or final_limit <= 0:
        return [], {"window_size": 0, "alpha": 0.85}

    alpha = 0.85
    window_size = choose_mmr_window_size(final_limit)
    selected: list[dict] = []
    remaining = [dict(item) for item in items]

    while remaining and len(selected) < final_limit:
        recent_window = selected[-window_size:]
        best_idx = 0
        best_score = float("-inf")

        for idx, item in enumerate(remaining):
            relevance = float(item.get("merged_score", item.get("recall_score", 0.0)))
            if recent_window:
                diversity_penalty = max(movie_similarity(item, chosen) for chosen in recent_window)
            else:
                diversity_penalty = 0.0
            rerank_score = alpha * relevance - (1.0 - alpha) * diversity_penalty
            if rerank_score > best_score:
                best_score = rerank_score
                best_idx = idx

        chosen = remaining.pop(best_idx)
        chosen["rerank_score"] = round(float(best_score), 6)
        chosen["rank_no"] = len(selected) + 1
        chosen["channel"] = "reranked"
        chosen["reason"] = f"婊戝姩绐楀彛閲嶆帓锛氱粨鍚堢浉鍏虫€т笌澶氭牱鎬э紝绐楀彛澶у皬 {window_size}"
        selected.append(chosen)

    return selected, {"window_size": window_size, "alpha": alpha}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/catalog/stats")
def get_catalog_stats():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total_movies
                FROM movies
                """
            )
            row = cur.fetchone()
    return {"total_movies": int(row["total_movies"]) if row else 0}


@app.post("/api/auth/login")
def login(payload: LoginRequest):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT app_user_id, username, display_name, password_hash, movielens_user_id
                FROM app_users
                WHERE username = %s
                """,
                (payload.username,),
            )
            row = cur.fetchone()

    if not row or row["password_hash"] != hash_password(payload.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = secrets.token_urlsafe(24)
    user = build_user_payload(row)
    SESSIONS[token] = user
    return {"token": token, "user": user}


@app.get("/api/auth/me")
def me(current_user: dict = Depends(get_current_user)):
    return {"user": current_user}


@app.get("/api/recommendations/popular")
def get_popular_movies(limit: int = 24, exclude_ids: Optional[str] = None):
    limit = max(1, min(limit, 60))
    excluded_movie_ids = parse_exclude_ids(exclude_ids)
    source_limit = min(max(limit + len(excluded_movie_ids), limit), 240)
    popular_items = get_popular_recommendations(source_limit)
    filtered_items = filter_items_by_excluded(popular_items, excluded_movie_ids, limit)
    return {
        "channel": "popular",
        "items": filtered_items,
        "excluded_count": len(excluded_movie_ids),
        "has_more": len(filter_items_by_excluded(popular_items, excluded_movie_ids)) > limit,
    }


@app.get("/api/recommendations/me")
def get_my_recommendations(
    limit: int = 20,
    next_batch: bool = False,
    current_user: dict = Depends(get_current_user),
):
    limit = max(1, min(limit, 60))
    cache_user_id = int(current_user["movielens_user_id"])

    if next_batch:
        excluded_movie_ids = set(advance_user_exposure_window(cache_user_id))
        current_next_batch_count = get_user_next_batch_count(cache_user_id) + 1
    else:
        excluded_movie_ids = set()
        current_next_batch_count = get_user_next_batch_count(cache_user_id)

    channel_limits = compute_recall_channel_limits(current_next_batch_count)
    rank_candidate_limit = compute_rank_candidate_limit(channel_limits)
    user_history_rows = load_user_positive_history_rows(current_user["movielens_user_id"], limit=80)
    seen_movie_ids = load_user_seen_movie_ids(current_user["movielens_user_id"])
    popular_pool_movie_ids = load_popular_pool_movie_ids()
    popular_items_raw = get_popular_recommendations(channel_limits["popular"])
    genre_items_raw, genre_profile = get_genre_recall_recommendations(
        current_user["movielens_user_id"],
        channel_limits["genre"],
        user_history_rows,
        seen_movie_ids,
    )
    long_tail_items_raw = get_long_tail_recall_recommendations(
        channel_limits["long_tail"],
        genre_profile,
        seen_movie_ids,
        popular_pool_movie_ids,
    )
    itemcf_items_raw, positive_seed_count = get_itemcf_recommendations(
        current_user["movielens_user_id"],
        channel_limits["itemcf"],
    )
    twotower_items_raw, twotower_positive_count = get_twotower_recommendations(
        current_user["movielens_user_id"],
        channel_limits["twotower"],
    )
    popular_items = filter_items_by_excluded(popular_items_raw, excluded_movie_ids, limit)
    genre_items = filter_items_by_excluded(genre_items_raw, excluded_movie_ids, limit)
    long_tail_items = filter_items_by_excluded(long_tail_items_raw, excluded_movie_ids, limit)
    itemcf_items = filter_items_by_excluded(itemcf_items_raw, excluded_movie_ids, limit)
    twotower_items = filter_items_by_excluded(twotower_items_raw, excluded_movie_ids, limit)
    merged_candidates = merge_recall_results_v2(
        popular_items_raw,
        genre_items_raw,
        long_tail_items_raw,
        itemcf_items_raw,
        twotower_items_raw,
        rank_candidate_limit,
    )
    merged_candidates = filter_items_by_excluded(merged_candidates, excluded_movie_ids, rank_candidate_limit)
    pre_rank_input_count = len(merged_candidates)
    merged_candidates = merged_candidates[:rank_candidate_limit]
    ranked_candidates, ranker_meta = rank_candidates_with_lgb(
        popular_items_raw,
        genre_items_raw,
        long_tail_items_raw,
        itemcf_items_raw,
        twotower_items_raw,
        merged_candidates,
        user_history_rows,
    )
    merged_items, rerank_meta = rerank_with_sliding_window_mmr(ranked_candidates, limit)
    save_user_last_delivered_ids(cache_user_id, merged_items)
    if next_batch:
        current_next_batch_count = increment_user_next_batch_count(cache_user_id)
    rerank_meta.update(ranker_meta)
    rerank_meta["excluded_count"] = len(excluded_movie_ids)
    rerank_meta["has_more"] = len(ranked_candidates) > limit
    rerank_meta["channel_limits"] = channel_limits
    rerank_meta["popular_raw_count"] = len(popular_items_raw)
    rerank_meta["genre_raw_count"] = len(genre_items_raw)
    rerank_meta["long_tail_raw_count"] = len(long_tail_items_raw)
    rerank_meta["itemcf_raw_count"] = len(itemcf_items_raw)
    rerank_meta["twotower_raw_count"] = len(twotower_items_raw)
    rerank_meta["merged_candidate_limit"] = rank_candidate_limit
    rerank_meta["pre_rank_input_count"] = pre_rank_input_count
    rerank_meta["twotower_ann_enabled"] = bool(load_twotower_movie_cache().get("faiss_enabled"))
    rerank_meta["exposure_window_size"] = len(USER_EXPOSURE_WINDOWS.get(cache_user_id, []))
    rerank_meta["exposure_window_limit"] = EXPOSURE_WINDOW_SIZE
    rerank_meta["next_batch"] = next_batch
    rerank_meta["next_batch_count"] = current_next_batch_count

    return {
        "user": current_user,
        "positive_seed_count": positive_seed_count,
        "twotower_positive_count": twotower_positive_count,
        "popular": popular_items,
        "genre": genre_items,
        "long_tail": long_tail_items,
        "itemcf": itemcf_items,
        "twotower": twotower_items,
        "merged_raw": ranked_candidates[:limit],
        "merged": merged_items,
        "rerank_meta": rerank_meta,
    }


@app.get("/api/users/me/ratings")
def get_my_ratings(limit: int = 30, current_user: dict = Depends(get_current_user)):
    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    r.movie_id,
                    r.rating,
                    r.rating_timestamp,
                    m.tmdb_title,
                    m.original_title,
                    m.poster_url,
                    m.release_date,
                    m.vote_average,
                    m.tmdb_genres
                FROM ratings r
                JOIN movies m
                    ON m.movie_id = r.movie_id
                WHERE r.user_id = %s
                ORDER BY r.rating_timestamp DESC, r.movie_id DESC
                LIMIT %s
                """,
                (current_user["movielens_user_id"], limit),
            )
            rows = cur.fetchall()

    return {
        "items": [
            {
                "movie_id": row["movie_id"],
                "rating": float(row["rating"]),
                "rating_timestamp": row["rating_timestamp"],
                "title": row["tmdb_title"] or row["original_title"],
                "original_title": row["original_title"],
                "poster_url": row["poster_url"],
                "release_date": row["release_date"].isoformat() if row["release_date"] else None,
                "vote_average": float(row["vote_average"]) if row["vote_average"] is not None else None,
                "genres": parse_genres(row["tmdb_genres"]),
            }
            for row in rows
        ]
    }


@app.get("/api/users/me/ratings/{movie_id}")
def get_my_rating(movie_id: int, current_user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rating, rating_timestamp
                FROM ratings
                WHERE user_id = %s AND movie_id = %s
                """,
                (current_user["movielens_user_id"], movie_id),
            )
            row = cur.fetchone()

    if not row:
        return {"movie_id": movie_id, "rating": None}

    return {
        "movie_id": movie_id,
        "rating": float(row["rating"]),
        "rating_timestamp": row["rating_timestamp"],
    }


@app.put("/api/users/me/ratings/{movie_id}")
def put_my_rating(movie_id: int, payload: RatingRequest, current_user: dict = Depends(get_current_user)):
    ensure_half_step(payload.rating)
    rating_timestamp = int(time.time())
    global TWOTOWER_MOVIE_CACHE, LGB_RANKER_CACHE
    TWOTOWER_MOVIE_CACHE = None
    LGB_RANKER_CACHE = None
    reset_user_exposure_state(int(current_user["movielens_user_id"]))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT movie_id FROM movies WHERE movie_id = %s", (movie_id,))
            movie_exists = cur.fetchone()
            cur.execute(
                """
                SELECT rating
                FROM ratings
                WHERE user_id = %s AND movie_id = %s
                """,
                (current_user["movielens_user_id"], movie_id),
            )
            existing_row = cur.fetchone()
            previous_rating = float(existing_row["rating"]) if existing_row else None
            if not movie_exists:
                raise HTTPException(status_code=404, detail="电影不存在")

            cur.execute(
                """
                INSERT INTO ratings (user_id, movie_id, rating, rating_timestamp)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, movie_id)
                DO UPDATE SET
                    rating = EXCLUDED.rating,
                    rating_timestamp = EXCLUDED.rating_timestamp
                """,
                (
                    current_user["movielens_user_id"],
                    movie_id,
                    payload.rating,
                    rating_timestamp,
                ),
            )
            log_rating_event(
                cur,
                user_id=int(current_user["movielens_user_id"]),
                movie_id=movie_id,
                event_type="upsert",
                previous_rating=previous_rating,
                new_rating=float(payload.rating),
                event_ts=rating_timestamp,
            )
        conn.commit()

    return {
        "movie_id": movie_id,
        "rating": payload.rating,
        "rating_timestamp": rating_timestamp,
    }


@app.delete("/api/users/me/ratings/{movie_id}")
def delete_my_rating(movie_id: int, current_user: dict = Depends(get_current_user)):
    global TWOTOWER_MOVIE_CACHE, LGB_RANKER_CACHE
    TWOTOWER_MOVIE_CACHE = None
    LGB_RANKER_CACHE = None
    reset_user_exposure_state(int(current_user["movielens_user_id"]))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rating
                FROM ratings
                WHERE user_id = %s AND movie_id = %s
                """,
                (current_user["movielens_user_id"], movie_id),
            )
            existing_row = cur.fetchone()
            previous_rating = float(existing_row["rating"]) if existing_row else None
            event_ts = int(time.time())
            cur.execute(
                """
                DELETE FROM ratings
                WHERE user_id = %s AND movie_id = %s
                RETURNING movie_id
                """,
                (current_user["movielens_user_id"], movie_id),
            )
            row = cur.fetchone()
            if row and previous_rating is not None:
                log_rating_event(
                    cur,
                    user_id=int(current_user["movielens_user_id"]),
                    movie_id=movie_id,
                    event_type="delete",
                    previous_rating=previous_rating,
                    new_rating=None,
                    event_ts=event_ts,
                )
        conn.commit()

    if not row:
        raise HTTPException(status_code=404, detail="评分记录不存在")

    return {
        "movie_id": movie_id,
        "deleted": True,
    }


@app.get("/api/internal/jobs/{job_name}")
def get_internal_job(job_name: str):
    if job_name not in INTERNAL_JOB_SCRIPTS:
        raise HTTPException(status_code=404, detail="job not found")
    state = build_internal_job_response(job_name)
    if state is None:
        return {
            "job_name": job_name,
            "status": "idle",
            "log_tail": "",
        }
    return state


@app.post("/api/internal/jobs/{job_name}/start")
def start_internal_job(job_name: str, payload: OfflineJobStartRequest):
    if job_name not in INTERNAL_JOB_SCRIPTS:
        raise HTTPException(status_code=404, detail="job not found")

    current_state = build_internal_job_response(job_name)
    if current_state and current_state.get("status") in {"starting", "running"}:
        return current_state

    run_id = uuid.uuid4().hex[:12]
    script_name = INTERNAL_JOB_SCRIPTS[job_name]
    worker = threading.Thread(
        target=run_internal_job,
        args=(job_name, run_id, script_name, payload.env_overrides),
        daemon=True,
    )
    worker.start()
    time.sleep(0.2)
    return build_internal_job_response(job_name) or {
        "job_name": job_name,
        "run_id": run_id,
        "status": "starting",
        "log_tail": "",
    }
