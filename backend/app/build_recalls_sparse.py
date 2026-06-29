import math
import os
import time
from array import array

import numpy as np
import psycopg
from psycopg.rows import dict_row
from scipy import sparse


DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "movie_rec")
DB_USER = os.getenv("DB_USER", "movie_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "movie_pass")
DB_DSN = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"

POSITIVE_THRESHOLD = float(os.getenv("ITEMCF_POSITIVE_THRESHOLD", "4.0"))
MIN_LIKE_USERS = int(os.getenv("ITEMCF_MIN_LIKE_USERS", "3"))
MIN_CO_LIKE = int(os.getenv("ITEMCF_MIN_CO_LIKE", "2"))
TOP_K = int(os.getenv("ITEMCF_TOP_K", "80"))
FETCH_SIZE = int(os.getenv("ITEMCF_FETCH_SIZE", "200000"))


POPULAR_RECALL_SQL = """
INSERT INTO recall_popular_movies (
    movie_id,
    recall_score,
    rank_no,
    rating_count,
    avg_rating
)
WITH global_stats AS (
    SELECT
        CASE
            WHEN COALESCE(SUM(rating_count), 0) > 0
                THEN (SUM(rating_sum) / SUM(rating_count))::NUMERIC(4,3)
            ELSE 0::NUMERIC(4,3)
        END AS global_avg
    FROM movie_rating_stats
),
movie_stats AS (
    SELECT
        m.movie_id,
        COALESCE(s.rating_count, 0)::INTEGER AS rating_count,
        s.avg_rating::NUMERIC(4,3) AS avg_rating,
        COALESCE(MAX(m.vote_average), 0)::NUMERIC(6,3) AS tmdb_vote_average,
        COALESCE(MAX(m.popularity), 0)::NUMERIC(12,4) AS tmdb_popularity
    FROM movies m
    LEFT JOIN movie_rating_stats s
        ON s.movie_id = m.movie_id
    GROUP BY m.movie_id, s.rating_count, s.avg_rating
),
scored_popular AS (
    SELECT
        ms.movie_id,
        ms.rating_count,
        ms.avg_rating,
        (
            (
                (ms.rating_count::NUMERIC / (ms.rating_count + 20))
                * COALESCE(ms.avg_rating, gs.global_avg)
            ) + (
                (20::NUMERIC / (ms.rating_count + 20))
                * gs.global_avg
            ) + 0.12 * LN(1 + ms.rating_count)
              + 0.03 * COALESCE(ms.tmdb_vote_average, 0)
              + 0.02 * LN(1 + COALESCE(ms.tmdb_popularity, 0))
        )::NUMERIC(12,6) AS recall_score
    FROM movie_stats ms
    CROSS JOIN global_stats gs
    WHERE ms.rating_count > 0
),
popular_ranked AS (
    SELECT
        movie_id,
        rating_count,
        avg_rating,
        recall_score,
        ROW_NUMBER() OVER (ORDER BY recall_score DESC, rating_count DESC, movie_id DESC) AS rank_no
    FROM scored_popular
)
SELECT
    movie_id,
    recall_score,
    rank_no,
    rating_count,
    avg_rating
FROM popular_ranked;
"""


def get_conn():
    return psycopg.connect(DB_DSN, row_factory=dict_row)


def rebuild_movie_rating_stats(cur: psycopg.Cursor) -> None:
    start = time.perf_counter()
    cur.execute("TRUNCATE TABLE movie_rating_stats")
    cur.execute(
        """
        INSERT INTO movie_rating_stats (
            movie_id,
            rating_count,
            rating_sum,
            avg_rating
        )
        SELECT
            movie_id,
            COUNT(*)::INTEGER AS rating_count,
            SUM(rating)::NUMERIC(12,3) AS rating_sum,
            AVG(rating)::NUMERIC(6,3) AS avg_rating
        FROM ratings
        GROUP BY movie_id
        """
    )
    elapsed = time.perf_counter() - start
    cur.execute("SELECT COUNT(*) AS cnt FROM movie_rating_stats")
    count = cur.fetchone()["cnt"]
    print(f"[rating_stats] rebuilt_rows={count} elapsed={elapsed:.2f}s")


def build_popular_recall(cur: psycopg.Cursor) -> None:
    start = time.perf_counter()
    cur.execute("TRUNCATE TABLE recall_popular_movies")
    cur.execute(POPULAR_RECALL_SQL)
    elapsed = time.perf_counter() - start
    cur.execute("SELECT COUNT(*) AS cnt FROM recall_popular_movies")
    count = cur.fetchone()["cnt"]
    print(f"[popular] inserted_rows={count} elapsed={elapsed:.2f}s")


def load_valid_movie_support(cur: psycopg.Cursor) -> tuple[np.ndarray, np.ndarray]:
    cur.execute(
        """
        SELECT
            r.movie_id,
            COUNT(*)::INTEGER AS like_users
        FROM ratings r
        INNER JOIN movies m
            ON m.movie_id = r.movie_id
        WHERE r.rating >= %s
          AND m.poster_url IS NOT NULL
        GROUP BY r.movie_id
        HAVING COUNT(*) >= %s
        ORDER BY r.movie_id
        """,
        (POSITIVE_THRESHOLD, MIN_LIKE_USERS),
    )
    rows = cur.fetchall()
    movie_ids = np.asarray([row["movie_id"] for row in rows], dtype=np.int32)
    supports = np.asarray([row["like_users"] for row in rows], dtype=np.int32)
    return movie_ids, supports


def stream_positive_matrix(movie_index: dict[int, int]) -> tuple[sparse.csr_matrix, int, int]:
    row_ids = array("I")
    col_ids = array("I")
    user_count = 0
    nnz = 0
    current_user_id = None
    last_log_nnz = 0

    with get_conn() as conn:
        with conn.cursor(name="itemcf_positive_cursor", row_factory=dict_row) as cur:
            cur.itersize = FETCH_SIZE
            cur.execute(
                """
                WITH valid_movies AS (
                    SELECT
                        r.movie_id
                    FROM ratings r
                    INNER JOIN movies m
                        ON m.movie_id = r.movie_id
                    WHERE r.rating >= %s
                      AND m.poster_url IS NOT NULL
                    GROUP BY r.movie_id
                    HAVING COUNT(*) >= %s
                )
                SELECT
                    r.user_id,
                    r.movie_id
                FROM ratings r
                INNER JOIN valid_movies vm
                    ON vm.movie_id = r.movie_id
                WHERE r.rating >= %s
                ORDER BY r.user_id, r.movie_id
                """,
                (POSITIVE_THRESHOLD, MIN_LIKE_USERS, POSITIVE_THRESHOLD),
            )

            while True:
                batch = cur.fetchmany(FETCH_SIZE)
                if not batch:
                    break
                for row in batch:
                    user_id = row["user_id"]
                    if user_id != current_user_id:
                        current_user_id = user_id
                        user_count += 1
                    row_ids.append(user_count - 1)
                    col_ids.append(movie_index[row["movie_id"]])
                    nnz += 1
                if nnz - last_log_nnz >= 1_000_000:
                    print(f"[itemcf] streamed_positive_rows={nnz} users={user_count}")
                    last_log_nnz = nnz

    if nnz == 0:
        return sparse.csr_matrix((0, len(movie_index)), dtype=np.float32), user_count, nnz

    row_arr = np.asarray(row_ids, dtype=np.int32)
    col_arr = np.asarray(col_ids, dtype=np.int32)
    data_arr = np.ones(nnz, dtype=np.float32)
    matrix = sparse.csr_matrix(
        (data_arr, (row_arr, col_arr)),
        shape=(user_count, len(movie_index)),
        dtype=np.float32,
    )
    matrix.sum_duplicates()
    return matrix, user_count, nnz


def write_itemcf_rows(
    cur: psycopg.Cursor,
    movie_ids: np.ndarray,
    supports: np.ndarray,
    cooc_matrix: sparse.csr_matrix,
) -> int:
    movie_ids_arr = np.asarray(movie_ids, dtype=np.int32)
    supports_arr = np.asarray(supports, dtype=np.int32)
    inserted_rows = 0

    cur.execute("TRUNCATE TABLE recall_itemcf_similarity")
    with cur.copy(
        """
        COPY recall_itemcf_similarity (
            source_movie_id,
            target_movie_id,
            sim_score,
            co_like_users,
            source_like_users,
            target_like_users
        ) FROM STDIN
        """
    ) as copy:
        indptr = cooc_matrix.indptr
        indices = cooc_matrix.indices
        data = cooc_matrix.data

        for source_idx in range(len(movie_ids_arr)):
            start = indptr[source_idx]
            end = indptr[source_idx + 1]
            if start == end:
                continue

            target_indices = indices[start:end]
            co_like_values = data[start:end].astype(np.int32, copy=False)
            valid_mask = co_like_values >= MIN_CO_LIKE
            if not np.any(valid_mask):
                continue

            target_indices = target_indices[valid_mask]
            co_like_values = co_like_values[valid_mask]
            target_movie_ids = movie_ids_arr[target_indices]
            target_supports = supports_arr[target_indices]

            source_support_float = float(supports_arr[source_idx])
            sims = co_like_values.astype(np.float64) / np.sqrt(
                source_support_float * target_supports.astype(np.float64)
            )
            order = np.lexsort((-target_movie_ids, -co_like_values, -sims))
            top_positions = order[:TOP_K]

            source_movie_id = int(movie_ids_arr[source_idx])
            source_support = int(supports_arr[source_idx])
            for pos in top_positions:
                copy.write_row(
                    (
                        source_movie_id,
                        int(target_movie_ids[pos]),
                        round(float(sims[pos]), 6),
                        int(co_like_values[pos]),
                        source_support,
                        int(target_supports[pos]),
                    )
                )
                inserted_rows += 1

            if source_idx > 0 and source_idx % 1000 == 0:
                print(
                    "[itemcf] ranked_sources="
                    f"{source_idx}/{len(movie_ids_arr)} inserted_rows={inserted_rows}"
                )

    return inserted_rows


def build_itemcf_recall(cur: psycopg.Cursor) -> None:
    start = time.perf_counter()
    movie_ids, supports = load_valid_movie_support(cur)
    if len(movie_ids) == 0:
        cur.execute("TRUNCATE TABLE recall_itemcf_similarity")
        print("[itemcf] no valid movies found, truncated recall_itemcf_similarity")
        return

    movie_index = {movie_id: idx for idx, movie_id in enumerate(movie_ids.tolist())}
    matrix, user_count, nnz = stream_positive_matrix(movie_index)
    print(
        "[itemcf] positive_matrix "
        f"users={user_count} movies={matrix.shape[1]} nnz={nnz}"
    )

    cooc_start = time.perf_counter()
    cooc_matrix = (matrix.transpose().tocsr() @ matrix).tocsr()
    cooc_matrix.setdiag(0)
    cooc_matrix.eliminate_zeros()
    cooc_elapsed = time.perf_counter() - cooc_start
    print(
        "[itemcf] cooccurrence "
        f"nnz={cooc_matrix.nnz} elapsed={cooc_elapsed:.2f}s"
    )

    inserted_rows = write_itemcf_rows(cur, movie_ids, supports, cooc_matrix)
    elapsed = time.perf_counter() - start
    print(
        "[itemcf] inserted_rows="
        f"{inserted_rows} elapsed={elapsed:.2f}s avg_edges_per_movie={inserted_rows / max(len(movie_ids), 1):.2f}"
    )


def main() -> None:
    started_at = time.perf_counter()
    with get_conn() as conn:
        with conn.cursor() as cur:
            rebuild_movie_rating_stats(cur)
            build_popular_recall(cur)
            build_itemcf_recall(cur)
        conn.commit()
    elapsed = time.perf_counter() - started_at
    print(f"[recalls] completed elapsed={elapsed:.2f}s")


if __name__ == "__main__":
    main()
