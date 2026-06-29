import json
import math
import os
from dataclasses import dataclass

import numpy as np
import psycopg
from psycopg.rows import dict_row

from twotower_artifacts import save_twotower_artifacts


DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "movie_rec")
DB_USER = os.getenv("DB_USER", "movie_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "movie_pass")
DB_DSN = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"

POSITIVE_THRESHOLD = float(os.getenv("TWOTOWER_POSITIVE_THRESHOLD", "4.0"))
EMBED_DIM = int(os.getenv("TWOTOWER_EMBED_DIM", "32"))
EPOCHS = int(os.getenv("TWOTOWER_EPOCHS", "12"))
LEARNING_RATE = float(os.getenv("TWOTOWER_LR", "0.035"))
REG = float(os.getenv("TWOTOWER_REG", "0.0008"))
BIAS_REG = float(os.getenv("TWOTOWER_BIAS_REG", "0.0005"))
SEED = int(os.getenv("TWOTOWER_SEED", "20260621"))
NEG_PER_POS = int(os.getenv("TWOTOWER_NEG_PER_POS", "3"))
MIN_USER_POSITIVES = int(os.getenv("TWOTOWER_MIN_USER_POSITIVES", "5"))
MAX_USERS = int(os.getenv("TWOTOWER_MAX_USERS", "0"))
TRAIN_BATCH_SIZE = int(os.getenv("TWOTOWER_BATCH_SIZE", "2048"))


@dataclass
class TrainingArtifacts:
    user_ids: list[int]
    movie_ids: list[int]
    user_embeddings: np.ndarray
    movie_embeddings: np.ndarray
    item_bias: np.ndarray
    user_positive_counts: list[int]
    movie_positive_counts: list[int]


def get_conn():
    return psycopg.connect(DB_DSN, row_factory=dict_row)


def sigmoid_neg(value: float) -> float:
    if value >= 0:
        exp_neg = math.exp(-value)
        sig = 1.0 / (1.0 + exp_neg)
    else:
        exp_pos = math.exp(value)
        sig = exp_pos / (1.0 + exp_pos)
    return 1.0 - sig


def sigmoid_neg_array(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(np.clip(values, -30.0, 30.0)))


def load_positive_pairs():
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
                    SELECT r.user_id, r.movie_id
                    FROM ratings r
                    INNER JOIN eligible_users eu
                        ON eu.user_id = r.user_id
                    WHERE r.rating >= %s
                    ORDER BY user_id, movie_id
                    """,
                    (POSITIVE_THRESHOLD, MIN_USER_POSITIVES, MAX_USERS, POSITIVE_THRESHOLD),
                )
            else:
                cur.execute(
                    """
                    WITH eligible_users AS (
                        SELECT user_id
                        FROM ratings
                        WHERE rating >= %s
                        GROUP BY user_id
                        HAVING COUNT(*) >= %s
                    )
                    SELECT r.user_id, r.movie_id
                    FROM ratings r
                    INNER JOIN eligible_users eu
                        ON eu.user_id = r.user_id
                    WHERE r.rating >= %s
                    ORDER BY user_id, movie_id
                    """,
                    (POSITIVE_THRESHOLD, MIN_USER_POSITIVES, POSITIVE_THRESHOLD),
                )
            rows = cur.fetchall()

    user_to_movies: dict[int, list[int]] = {}
    movie_positive_counts: dict[int, int] = {}
    for row in rows:
        user_id = row["user_id"]
        movie_id = row["movie_id"]
        user_to_movies.setdefault(user_id, []).append(movie_id)
        movie_positive_counts[movie_id] = movie_positive_counts.get(movie_id, 0) + 1

    if MAX_USERS > 0:
        print(
            f"[twotower] using top_users={len(user_to_movies)} "
            f"min_user_positives={MIN_USER_POSITIVES}"
        )

    user_ids = sorted(user_to_movies.keys())
    movie_ids = sorted(movie_positive_counts.keys())
    return user_ids, movie_ids, user_to_movies, movie_positive_counts


def sample_batch_negatives(
    batch_users: np.ndarray,
    batch_pos_movies: np.ndarray,
    user_positive_sets: list[set[int]],
    n_movies: int,
    negative_probs: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    batch_size = int(len(batch_users))
    if batch_size <= 1:
        negatives = np.empty(batch_size, dtype=np.int32)
        for idx, user_idx in enumerate(batch_users.tolist()):
            neg_movie_idx = int(rng.choice(n_movies, p=negative_probs))
            positive_set = user_positive_sets[int(user_idx)]
            while neg_movie_idx in positive_set:
                neg_movie_idx = int(rng.choice(n_movies, p=negative_probs))
            negatives[idx] = neg_movie_idx
        return negatives

    negatives = batch_pos_movies[rng.permutation(batch_size)].copy()
    invalid_mask = np.fromiter(
        (
            int(neg_movie_idx) in user_positive_sets[int(user_idx)]
            for user_idx, neg_movie_idx in zip(batch_users.tolist(), negatives.tolist())
        ),
        dtype=bool,
        count=batch_size,
    )
    if not invalid_mask.any():
        return negatives.astype(np.int32, copy=False)

    invalid_indices = np.flatnonzero(invalid_mask)
    for idx in invalid_indices.tolist():
        user_idx = int(batch_users[idx])
        positive_set = user_positive_sets[user_idx]
        neg_movie_idx = int(rng.choice(n_movies, p=negative_probs))
        while neg_movie_idx in positive_set:
            neg_movie_idx = int(rng.choice(n_movies, p=negative_probs))
        negatives[idx] = neg_movie_idx
    return negatives.astype(np.int32, copy=False)


def train_two_tower() -> TrainingArtifacts:
    rng = np.random.default_rng(SEED)

    user_ids, movie_ids, user_to_movies, movie_positive_counts = load_positive_pairs()
    user_index = {user_id: idx for idx, user_id in enumerate(user_ids)}
    movie_index = {movie_id: idx for idx, movie_id in enumerate(movie_ids)}

    user_positive_sets: list[set[int]] = []
    training_pairs: list[tuple[int, int]] = []
    user_positive_counts: list[int] = []
    for user_id in user_ids:
        movie_idx_set = {movie_index[movie_id] for movie_id in user_to_movies[user_id] if movie_id in movie_index}
        user_positive_sets.append(movie_idx_set)
        user_positive_counts.append(len(movie_idx_set))
        for movie_idx in movie_idx_set:
            training_pairs.append((user_index[user_id], movie_idx))

    n_users = len(user_ids)
    n_movies = len(movie_ids)
    print(
        f"[twotower] training with users={n_users} movies={n_movies} "
        f"positive_pairs={len(training_pairs)} dim={EMBED_DIM}"
    )
    print(f"[twotower] batch_negative_sampling batch_size={TRAIN_BATCH_SIZE} neg_per_pos={NEG_PER_POS}")

    user_embeddings = rng.normal(0, 0.1, size=(n_users, EMBED_DIM)).astype(np.float32)
    movie_embeddings = rng.normal(0, 0.1, size=(n_movies, EMBED_DIM)).astype(np.float32)
    item_bias = np.zeros(n_movies, dtype=np.float32)
    movie_popularity = np.asarray(
        [max(movie_positive_counts[movie_id], 1) for movie_id in movie_ids],
        dtype=np.float64,
    )
    negative_probs = np.power(movie_popularity, 0.75)
    negative_probs = negative_probs / negative_probs.sum()

    pairs_array = np.array(training_pairs, dtype=np.int32)

    for epoch in range(EPOCHS):
        rng.shuffle(pairs_array)
        total_loss = 0.0

        for batch_start in range(0, len(pairs_array), TRAIN_BATCH_SIZE):
            batch_pairs = pairs_array[batch_start:batch_start + TRAIN_BATCH_SIZE]
            batch_users = batch_pairs[:, 0]
            batch_pos_movies = batch_pairs[:, 1]
            if batch_users.size == 0:
                continue

            for _neg_step in range(NEG_PER_POS):
                batch_neg_movies = sample_batch_negatives(
                    batch_users,
                    batch_pos_movies,
                    user_positive_sets,
                    n_movies,
                    negative_probs,
                    rng,
                )

                user_old = user_embeddings[batch_users].copy()
                pos_old = movie_embeddings[batch_pos_movies].copy()
                neg_old = movie_embeddings[batch_neg_movies].copy()
                pos_bias_old = item_bias[batch_pos_movies].copy()
                neg_bias_old = item_bias[batch_neg_movies].copy()

                score_margin = pos_bias_old - neg_bias_old + np.sum(
                    user_old * (pos_old - neg_old),
                    axis=1,
                )
                grad = sigmoid_neg_array(score_margin).astype(np.float32, copy=False)
                total_loss += float(np.sum(np.log1p(np.exp(-np.clip(score_margin, -30.0, 30.0)))))

                user_delta = LEARNING_RATE * (
                    grad[:, None] * (pos_old - neg_old) - REG * user_old
                )
                pos_delta = LEARNING_RATE * (grad[:, None] * user_old - REG * pos_old)
                neg_delta = LEARNING_RATE * (-grad[:, None] * user_old - REG * neg_old)
                pos_bias_delta = LEARNING_RATE * (grad - BIAS_REG * pos_bias_old)
                neg_bias_delta = LEARNING_RATE * (-grad - BIAS_REG * neg_bias_old)

                np.add.at(user_embeddings, batch_users, user_delta)
                np.add.at(movie_embeddings, batch_pos_movies, pos_delta)
                np.add.at(movie_embeddings, batch_neg_movies, neg_delta)
                np.add.at(item_bias, batch_pos_movies, pos_bias_delta)
                np.add.at(item_bias, batch_neg_movies, neg_bias_delta)

        average_loss = total_loss / max(len(pairs_array) * NEG_PER_POS, 1)
        print(f"[twotower] epoch={epoch + 1}/{EPOCHS} avg_bpr_loss={average_loss:.6f}")

    movie_positive_counts_list = [movie_positive_counts[movie_id] for movie_id in movie_ids]

    return TrainingArtifacts(
        user_ids=user_ids,
        movie_ids=movie_ids,
        user_embeddings=user_embeddings,
        movie_embeddings=movie_embeddings,
        item_bias=item_bias,
        user_positive_counts=user_positive_counts,
        movie_positive_counts=movie_positive_counts_list,
    )


def serialize_vector(vector: np.ndarray) -> str:
    return json.dumps([round(float(value), 6) for value in vector.tolist()], ensure_ascii=False)


def vector_norm(vector: np.ndarray) -> float:
    return float(np.linalg.norm(vector))


def persist_artifacts(artifacts: TrainingArtifacts) -> None:
    user_rows = []
    for user_id, vector, positive_count in zip(
        artifacts.user_ids,
        artifacts.user_embeddings,
        artifacts.user_positive_counts,
    ):
        user_rows.append(
            (
                user_id,
                serialize_vector(vector),
                vector_norm(vector),
                int(positive_count),
            )
        )

    movie_rows = []
    for movie_id, vector, bias, positive_count in zip(
        artifacts.movie_ids,
        artifacts.movie_embeddings,
        artifacts.item_bias,
        artifacts.movie_positive_counts,
    ):
        movie_rows.append(
            (
                movie_id,
                serialize_vector(vector),
                vector_norm(vector),
                round(float(bias), 6),
                int(positive_count),
            )
        )

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE recall_twotower_user_embeddings, recall_twotower_movie_embeddings")
            cur.executemany(
                """
                INSERT INTO recall_twotower_user_embeddings (
                    user_id,
                    embedding_json,
                    vector_norm,
                    positive_count
                )
                VALUES (%s, %s, %s, %s)
                """,
                user_rows,
            )
            cur.executemany(
                """
                INSERT INTO recall_twotower_movie_embeddings (
                    movie_id,
                    embedding_json,
                    vector_norm,
                    item_bias,
                    positive_user_count
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                movie_rows,
            )
        conn.commit()

    save_twotower_artifacts(
        user_ids=artifacts.user_ids,
        movie_ids=artifacts.movie_ids,
        user_embeddings=artifacts.user_embeddings,
        movie_embeddings=artifacts.movie_embeddings,
        item_bias=artifacts.item_bias,
        user_positive_counts=artifacts.user_positive_counts,
        movie_positive_counts=artifacts.movie_positive_counts,
    )

    print(
        f"[twotower] saved user_embeddings={len(user_rows)} "
        f"movie_embeddings={len(movie_rows)}"
    )


def main():
    artifacts = train_two_tower()
    persist_artifacts(artifacts)


if __name__ == "__main__":
    main()
