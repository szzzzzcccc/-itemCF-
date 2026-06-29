import json
import math
import os
from collections import defaultdict
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import psycopg
from psycopg.rows import dict_row

from lgb_ranker_utils import FEATURE_NAMES, build_lgb_candidate_rows, build_user_profile
from twotower_artifacts import load_twotower_artifacts as load_twotower_binary_artifacts


DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "movie_rec")
DB_USER = os.getenv("DB_USER", "movie_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "movie_pass")
DB_DSN = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"

POSITIVE_THRESHOLD = float(os.getenv("LGB_POSITIVE_THRESHOLD", "4.0"))
MIN_POSITIVES = int(os.getenv("LGB_MIN_POSITIVES", "5"))
CHANNEL_TOPN = int(os.getenv("LGB_CHANNEL_TOPN", "80"))
GENRE_TOPN = int(os.getenv("LGB_GENRE_TOPN", "60"))
LONG_TAIL_TOPN = int(os.getenv("LGB_LONG_TAIL_TOPN", "20"))
POPULAR_POOL_LIMIT = int(os.getenv("LGB_POPULAR_POOL_LIMIT", "200"))
LONG_TAIL_MIN_VOTE_AVERAGE = float(os.getenv("LGB_LONG_TAIL_MIN_VOTE_AVERAGE", "6.5"))
LONG_TAIL_MIN_VOTE_COUNT = int(os.getenv("LGB_LONG_TAIL_MIN_VOTE_COUNT", "100"))
SEED = int(os.getenv("LGB_SEED", "20260623"))
MAX_USERS = int(os.getenv("LGB_MAX_USERS", "0"))
ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts_runtime"
MODEL_PATH = ARTIFACT_DIR / "lgb_ranker.joblib"
META_PATH = ARTIFACT_DIR / "lgb_ranker_meta.json"
SAMPLES_PATH = ARTIFACT_DIR / "lgb_ranker_samples.npz"
SAMPLES_META_PATH = ARTIFACT_DIR / "lgb_ranker_samples_meta.json"


def get_conn():
    return psycopg.connect(DB_DSN, row_factory=dict_row)


def parse_genres(value: str | None) -> list[str]:
    if not value:
        return []
    return [genre for genre in value.split("|") if genre]


def clamp_score(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def quality_score(vote_average: float) -> float:
    return clamp_score((float(vote_average) - 6.0) / 2.5)


def stability_score(vote_count: int) -> float:
    if int(vote_count) <= 0:
        return 0.0
    return clamp_score(float(np.log1p(int(vote_count)) / np.log1p(3000)))


def build_genre_preference_profile(history_positive_rows: list[dict], movie_map: dict[int, dict]) -> dict[str, float]:
    if not history_positive_rows:
        return {}

    genre_scores: dict[str, float] = {}
    total_rows = len(history_positive_rows)
    for index, row in enumerate(history_positive_rows):
        movie = movie_map.get(row["movie_id"])
        if not movie:
            continue
        genres = movie.get("genres") or []
        if not genres:
            continue
        rating_weight = max(float(row.get("rating") or POSITIVE_THRESHOLD) - 3.0, 0.5)
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


def load_movie_map():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    movie_id,
                    tmdb_title,
                    original_title,
                    overview,
                    poster_url,
                    release_date,
                    vote_average,
                    vote_count,
                    popularity,
                    tmdb_genres
                FROM movies
                WHERE poster_url IS NOT NULL
                ORDER BY movie_id
                """
            )
            rows = cur.fetchall()

    movie_map = {}
    for row in rows:
        release_date = row["release_date"]
        movie_map[row["movie_id"]] = {
            "movie_id": row["movie_id"],
            "title": row["tmdb_title"] or row["original_title"],
            "overview": row["overview"],
            "poster_url": row["poster_url"],
            "release_date": release_date.isoformat() if release_date else None,
            "release_year": release_date.year if release_date else None,
            "vote_average": float(row["vote_average"]) if row["vote_average"] is not None else 0.0,
            "vote_count": int(row["vote_count"]) if row["vote_count"] is not None else 0,
            "popularity": float(row["popularity"]) if row["popularity"] is not None else 0.0,
            "genres": parse_genres(row["tmdb_genres"]),
        }
    return movie_map


def load_ratings_by_user(candidate_movie_ids: set[int]):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if MAX_USERS > 0:
                cur.execute(
                    """
                    WITH eligible_users AS (
                        SELECT user_id
                        FROM ratings
                        WHERE rating >= %s
                        GROUP BY user_id
                        HAVING COUNT(*) >= %s
                        ORDER BY COUNT(*) DESC, user_id
                        LIMIT %s
                    )
                    SELECT r.user_id, r.movie_id, r.rating, r.rating_timestamp
                    FROM ratings r
                    INNER JOIN eligible_users eu
                        ON eu.user_id = r.user_id
                    ORDER BY r.user_id, r.rating_timestamp, r.movie_id
                    """,
                    (POSITIVE_THRESHOLD, MIN_POSITIVES, MAX_USERS),
                )
            else:
                cur.execute(
                    """
                    SELECT user_id, movie_id, rating, rating_timestamp
                    FROM ratings
                    ORDER BY user_id, rating_timestamp, movie_id
                    """
                )
            rows = cur.fetchall()

    by_user: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        if row["movie_id"] in candidate_movie_ids:
            by_user[row["user_id"]].append(row)
    return by_user


def load_popular_items(movie_map: dict[int, dict]):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT movie_id, rank_no, recall_score
                FROM recall_popular_movies
                ORDER BY rank_no
                """
            )
            rows = cur.fetchall()

    items = []
    for row in rows:
        movie_id = row["movie_id"]
        if movie_id not in movie_map:
            continue
        payload = dict(movie_map[movie_id])
        payload.update(
            {
                "rank_no": int(row["rank_no"]),
                "recall_score": float(row["recall_score"]),
                "channel": "popular",
            }
        )
        items.append(payload)
    return items


def load_itemcf_map():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_movie_id, target_movie_id, sim_score, co_like_users
                FROM recall_itemcf_similarity
                ORDER BY source_movie_id, sim_score DESC, target_movie_id DESC
                """
            )
            rows = cur.fetchall()

    itemcf_map: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        itemcf_map[row["source_movie_id"]].append(
            {
                "target_movie_id": row["target_movie_id"],
                "sim_score": float(row["sim_score"]),
                "co_like_users": int(row["co_like_users"]),
            }
        )
    return itemcf_map


def load_twotower_artifacts(movie_map: dict[int, dict]):
    binary_artifacts = load_twotower_binary_artifacts()
    if binary_artifacts is not None:
        user_map = {
            int(user_id): {
                "embedding": binary_artifacts["user_embeddings"][idx],
                "positive_count": int(binary_artifacts["user_positive_counts"][idx]),
            }
            for idx, user_id in enumerate(binary_artifacts["user_ids"].tolist())
        }
        movie_ids = []
        embeddings = []
        biases = []
        payloads = []
        for idx, movie_id in enumerate(binary_artifacts["movie_ids"].tolist()):
            movie_id = int(movie_id)
            if movie_id not in movie_map:
                continue
            movie_ids.append(movie_id)
            embeddings.append(binary_artifacts["movie_embeddings"][idx])
            biases.append(float(binary_artifacts["item_bias"][idx]))
            payload = dict(movie_map[movie_id])
            payload["positive_user_count"] = int(binary_artifacts["movie_positive_counts"][idx])
            payloads.append(payload)
        return {
            "user_map": user_map,
            "movie_ids": np.asarray(movie_ids, dtype=np.int32),
            "embeddings": np.vstack(embeddings).astype(np.float32) if embeddings else np.empty((0, 0), dtype=np.float32),
            "biases": np.asarray(biases, dtype=np.float32),
            "payloads": payloads,
        }

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, embedding_json, positive_count
                FROM recall_twotower_user_embeddings
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

    user_map = {
        row["user_id"]: {
            "embedding": np.asarray(json.loads(row["embedding_json"]), dtype=np.float32),
            "positive_count": int(row["positive_count"]),
        }
        for row in user_rows
    }

    movie_ids = []
    embeddings = []
    biases = []
    payloads = []
    for row in movie_rows:
        movie_id = row["movie_id"]
        if movie_id not in movie_map:
            continue
        movie_ids.append(movie_id)
        embeddings.append(np.asarray(json.loads(row["embedding_json"]), dtype=np.float32))
        biases.append(float(row["item_bias"]))
        payload = dict(movie_map[movie_id])
        payload["positive_user_count"] = int(row["positive_user_count"])
        payloads.append(payload)

    return {
        "user_map": user_map,
        "movie_ids": np.asarray(movie_ids, dtype=np.int32),
        "embeddings": np.vstack(embeddings).astype(np.float32) if embeddings else np.empty((0, 0), dtype=np.float32),
        "biases": np.asarray(biases, dtype=np.float32),
        "payloads": payloads,
    }


def get_itemcf_items(seed_movies: set[int], seen_movies: set[int], itemcf_map: dict[int, list[dict]], movie_map: dict[int, dict]):
    scored: dict[int, float] = defaultdict(float)
    best_sim: dict[int, float] = defaultdict(float)
    support: dict[int, int] = defaultdict(int)
    for seed_movie in seed_movies:
        for edge in itemcf_map.get(seed_movie, []):
            target_id = edge["target_movie_id"]
            if target_id in seen_movies or target_id not in movie_map:
                continue
            sim = edge["sim_score"]
            scored[target_id] += sim
            best_sim[target_id] = max(best_sim[target_id], sim)
            support[target_id] += 1

    ranked = sorted(scored.items(), key=lambda pair: (pair[1], best_sim[pair[0]], pair[0]), reverse=True)
    items = []
    for rank, (movie_id, score) in enumerate(ranked[:CHANNEL_TOPN], start=1):
        payload = dict(movie_map[movie_id])
        payload.update(
            {
                "recall_score": float(score),
                "best_similarity": float(best_sim[movie_id]),
                "supporting_seed_count": int(support[movie_id]),
                "rank_no": rank,
                "channel": "itemcf",
            }
        )
        items.append(payload)
    return items


def get_twotower_items(user_id: int, seen_movies: set[int], twotower_artifacts: dict):
    user_entry = twotower_artifacts["user_map"].get(user_id)
    if not user_entry:
        return []
    scores = twotower_artifacts["embeddings"] @ user_entry["embedding"] + twotower_artifacts["biases"]
    ranked = np.argsort(scores)[::-1]
    items = []
    for rank, idx in enumerate(ranked, start=1):
        movie_id = int(twotower_artifacts["movie_ids"][idx])
        if movie_id in seen_movies:
            continue
        payload = dict(twotower_artifacts["payloads"][idx])
        payload.update(
            {
                "recall_score": float(scores[idx]),
                "rank_no": rank,
                "channel": "twotower",
            }
        )
        items.append(payload)
        if len(items) >= CHANNEL_TOPN:
            break
    return items


def get_popular_items(popular_items_all: list[dict], seen_movies: set[int]):
    items = []
    rank = 0
    for item in popular_items_all:
        if item["movie_id"] in seen_movies:
            continue
        rank += 1
        payload = dict(item)
        payload["rank_no"] = rank
        items.append(payload)
        if len(items) >= CHANNEL_TOPN:
            break
    return items


def get_genre_items(
    history_positive_rows: list[dict],
    seen_movies: set[int],
    movie_map: dict[int, dict],
):
    genre_profile = build_genre_preference_profile(history_positive_rows, movie_map)
    if not genre_profile:
        return [], {}

    candidate_genres = list(genre_profile.keys())[:4]
    candidate_genre_set = set(candidate_genres)
    ranked = []
    for movie_id, movie in movie_map.items():
        if movie_id in seen_movies:
            continue
        genres = movie.get("genres") or []
        if not genres or not (candidate_genre_set & set(genres)):
            continue
        genre_match = compute_genre_match_score(genres, genre_profile)
        if genre_match <= 0.0:
            continue
        movie_quality = quality_score(movie.get("vote_average", 0.0))
        movie_stability = stability_score(movie.get("vote_count", 0))
        recall_score = 0.60 * genre_match + 0.25 * movie_quality + 0.15 * movie_stability
        payload = dict(movie)
        payload.update(
            {
                "recall_score": float(recall_score),
                "genre_match_score": float(genre_match),
                "quality_score": float(movie_quality),
                "stability_score": float(movie_stability),
                "channel": "genre",
            }
        )
        ranked.append(payload)

    ranked.sort(
        key=lambda item: (
            item["recall_score"],
            item.get("genre_match_score", 0.0),
            item.get("vote_average", 0.0),
            item["movie_id"],
        ),
        reverse=True,
    )
    items = []
    for rank, payload in enumerate(ranked[:GENRE_TOPN], start=1):
        item = dict(payload)
        item["rank_no"] = rank
        items.append(item)
    return items, genre_profile


def get_long_tail_items(
    genre_profile: dict[str, float],
    seen_movies: set[int],
    movie_map: dict[int, dict],
    popular_pool_movie_ids: set[int],
):
    if not genre_profile:
        return []

    candidate_genres = list(genre_profile.keys())[:3]
    candidate_genre_set = set(candidate_genres)
    candidate_movies = []
    for movie_id, movie in movie_map.items():
        if movie_id in seen_movies or movie_id in popular_pool_movie_ids:
            continue
        genres = movie.get("genres") or []
        if not genres or not (candidate_genre_set & set(genres)):
            continue
        if float(movie.get("vote_average", 0.0)) < LONG_TAIL_MIN_VOTE_AVERAGE:
            continue
        if int(movie.get("vote_count", 0)) < LONG_TAIL_MIN_VOTE_COUNT:
            continue
        candidate_movies.append((movie_id, movie))

    if not candidate_movies:
        return []

    rows_sorted_by_popularity = sorted(
        candidate_movies,
        key=lambda pair: (float(pair[1].get("popularity", 0.0)), pair[0]),
        reverse=True,
    )
    total_candidates = len(rows_sorted_by_popularity)
    popularity_rank_map = {
        movie_id: rank
        for rank, (movie_id, _movie) in enumerate(rows_sorted_by_popularity, start=1)
    }

    ranked = []
    for movie_id, movie in candidate_movies:
        genres = movie.get("genres") or []
        genre_match = compute_genre_match_score(genres, genre_profile)
        if genre_match <= 0.0:
            continue
        movie_quality = quality_score(movie.get("vote_average", 0.0))
        if total_candidates <= 1:
            long_tail_bonus = 1.0
        else:
            long_tail_bonus = (popularity_rank_map[movie_id] - 1) / (total_candidates - 1)
        recall_score = 0.45 * genre_match + 0.25 * movie_quality + 0.30 * long_tail_bonus
        payload = dict(movie)
        payload.update(
            {
                "recall_score": float(recall_score),
                "genre_match_score": float(genre_match),
                "quality_score": float(movie_quality),
                "long_tail_bonus": float(long_tail_bonus),
                "channel": "long_tail",
            }
        )
        ranked.append(payload)

    ranked.sort(
        key=lambda item: (
            item["recall_score"],
            item.get("long_tail_bonus", 0.0),
            item.get("vote_average", 0.0),
            item["movie_id"],
        ),
        reverse=True,
    )
    items = []
    for rank, payload in enumerate(ranked[:LONG_TAIL_TOPN], start=1):
        item = dict(payload)
        item["rank_no"] = rank
        items.append(item)
    return items


def merge_candidates(
    popular_items: list[dict],
    genre_items: list[dict],
    long_tail_items: list[dict],
    itemcf_items: list[dict],
    twotower_items: list[dict],
):
    merged_map: dict[int, dict] = {}
    for channel_name, items, weight in (
        ("twotower", twotower_items, 0.30),
        ("itemcf", itemcf_items, 0.27),
        ("genre", genre_items, 0.18),
        ("long_tail", long_tail_items, 0.10),
        ("popular", popular_items, 0.15),
    ):
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
    return sorted(
        merged_map.values(),
        key=lambda item: (item["merged_score"], item.get("recall_score", 0.0), item["movie_id"]),
        reverse=True,
    )


def build_training_groups():
    movie_map = load_movie_map()
    candidate_movie_ids = set(movie_map.keys())
    by_user = load_ratings_by_user(candidate_movie_ids)
    popular_items_all = load_popular_items(movie_map)
    popular_pool_movie_ids = {item["movie_id"] for item in popular_items_all[:POPULAR_POOL_LIMIT]}
    itemcf_map = load_itemcf_map()
    twotower_artifacts = load_twotower_artifacts(movie_map)

    x_rows = []
    y_rows = []
    groups = []
    sample_user_ids = []
    sample_target_movie_ids = []
    sample_candidate_movie_ids = []
    user_count = 0

    for user_id, interactions in by_user.items():
        positives = [row for row in interactions if float(row["rating"]) >= POSITIVE_THRESHOLD]
        if len(positives) < MIN_POSITIVES:
            continue

        target = positives[-1]
        history_positive_rows = positives[:-1]
        if not history_positive_rows:
            continue

        seen_before_target = set()
        reached_target = False
        for row in interactions:
            key = (row["movie_id"], row["rating_timestamp"])
            if not reached_target and key == (target["movie_id"], target["rating_timestamp"]):
                reached_target = True
            if not reached_target:
                seen_before_target.add(row["movie_id"])

        history_movie_rows = []
        for row in history_positive_rows:
            movie_id = row["movie_id"]
            if movie_id not in movie_map:
                continue
            movie_payload = dict(movie_map[movie_id])
            history_movie_rows.append(movie_payload)

        popular_items = get_popular_items(popular_items_all, seen_before_target)
        genre_items, genre_profile = get_genre_items(
            history_positive_rows,
            seen_before_target,
            movie_map,
        )
        long_tail_items = get_long_tail_items(
            genre_profile,
            seen_before_target,
            movie_map,
            popular_pool_movie_ids,
        )
        itemcf_items = get_itemcf_items(
            {row["movie_id"] for row in history_positive_rows},
            seen_before_target,
            itemcf_map,
            movie_map,
        )
        twotower_items = get_twotower_items(user_id, seen_before_target, twotower_artifacts)
        merged_candidates = merge_candidates(
            popular_items,
            genre_items,
            long_tail_items,
            itemcf_items,
            twotower_items,
        )

        if target["movie_id"] not in {item["movie_id"] for item in merged_candidates} and target["movie_id"] in movie_map:
            fallback = dict(movie_map[target["movie_id"]])
            fallback.update({"merged_score": 0.0, "source_channels": ["target_inject"]})
            merged_candidates.append(fallback)

        user_profile = build_user_profile(history_movie_rows)
        candidate_rows = build_lgb_candidate_rows(
            popular_items,
            genre_items,
            long_tail_items,
            itemcf_items,
            twotower_items,
            merged_candidates,
            user_profile,
        )
        labels = []
        features = []
        for row in candidate_rows:
            labels.append(1 if row["movie_id"] == target["movie_id"] else 0)
            features.append(row["features"])

        if sum(labels) == 0 or len(labels) < 5:
            continue

        x_rows.extend(features)
        y_rows.extend(labels)
        groups.append(len(labels))
        sample_user_ids.extend([int(user_id)] * len(labels))
        sample_target_movie_ids.extend([int(target["movie_id"])] * len(labels))
        sample_candidate_movie_ids.extend([int(row["movie_id"]) for row in candidate_rows])
        user_count += 1

    return {
        "x_rows": np.asarray(x_rows, dtype=np.float32),
        "y_rows": np.asarray(y_rows, dtype=np.float32),
        "groups": np.asarray(groups, dtype=np.int32),
        "sample_user_ids": np.asarray(sample_user_ids, dtype=np.int32),
        "sample_target_movie_ids": np.asarray(sample_target_movie_ids, dtype=np.int32),
        "sample_candidate_movie_ids": np.asarray(sample_candidate_movie_ids, dtype=np.int32),
        "user_count": user_count,
    }


def build_and_save_training_samples():
    sample_data = build_training_groups()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    x_rows = sample_data["x_rows"]
    y_rows = sample_data["y_rows"]
    groups = sample_data["groups"]
    user_count = int(sample_data["user_count"])
    if len(x_rows) == 0 or len(groups) == 0:
        raise RuntimeError("No training samples were generated for LightGBM ranker.")

    np.savez_compressed(
        SAMPLES_PATH,
        x_rows=x_rows,
        y_rows=y_rows,
        groups=groups,
        sample_user_ids=sample_data["sample_user_ids"],
        sample_target_movie_ids=sample_data["sample_target_movie_ids"],
        sample_candidate_movie_ids=sample_data["sample_candidate_movie_ids"],
    )
    samples_meta = {
        "trained_user_groups": user_count,
        "train_samples": int(len(x_rows)),
        "feature_count": len(FEATURE_NAMES),
        "feature_names": FEATURE_NAMES,
    }
    SAMPLES_META_PATH.write_text(json.dumps(samples_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[lgb_samples] trained_user_groups={user_count} train_samples={len(x_rows)}")
    print(f"[lgb_samples] samples_saved={SAMPLES_PATH}")


def load_training_samples():
    if not SAMPLES_PATH.exists():
        raise RuntimeError(f"Training samples artifact not found: {SAMPLES_PATH}")
    with np.load(SAMPLES_PATH, allow_pickle=False) as data:
        return {
            "x_rows": data["x_rows"].astype(np.float32, copy=False),
            "y_rows": data["y_rows"].astype(np.float32, copy=False),
            "groups": data["groups"].astype(np.int32, copy=False),
        }


def main():
    sample_data = load_training_samples()
    x_rows = sample_data["x_rows"]
    y_rows = sample_data["y_rows"]
    groups = sample_data["groups"].tolist()

    model = lgb.LGBMRanker(
        objective="lambdarank",
        metric="ndcg",
        n_estimators=180,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=10,
        subsample=0.9,
        colsample_bytree=0.9,
        random_state=SEED,
    )
    model.fit(x_rows, y_rows, group=groups, feature_name=FEATURE_NAMES)
    joblib.dump(model, MODEL_PATH)

    samples_meta = {}
    if SAMPLES_META_PATH.exists():
        samples_meta = json.loads(SAMPLES_META_PATH.read_text(encoding="utf-8"))
    meta = {
        "trained_user_groups": int(samples_meta.get("trained_user_groups", len(groups))),
        "train_samples": int(len(x_rows)),
        "feature_count": len(FEATURE_NAMES),
        "feature_names": FEATURE_NAMES,
    }
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[lgb_ranker] trained_user_groups={meta['trained_user_groups']} train_samples={len(x_rows)}")
    print(f"[lgb_ranker] model_saved={MODEL_PATH}")


if __name__ == "__main__":
    main()
