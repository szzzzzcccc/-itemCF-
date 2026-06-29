import os
import time
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row


DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "movie_rec")
DB_USER = os.getenv("DB_USER", "movie_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "movie_pass")
DB_DSN = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"

JOB_NAME = os.getenv("RATING_EVENTS_JOB_NAME", "rating_events_incremental")
FETCH_BATCH_SIZE = int(os.getenv("RATING_EVENTS_FETCH_BATCH_SIZE", "5000"))

POPULAR_RECALL_FROM_STATS_SQL = """
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


def rebuild_movie_rating_stats(cur: psycopg.Cursor) -> int:
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
    cur.execute("SELECT COUNT(*) AS cnt FROM movie_rating_stats")
    return int(cur.fetchone()["cnt"])


def get_last_event_id(cur: psycopg.Cursor) -> int:
    cur.execute(
        """
        SELECT last_event_id
        FROM pipeline_watermarks
        WHERE job_name = %s
        """,
        (JOB_NAME,),
    )
    row = cur.fetchone()
    return int(row["last_event_id"]) if row else 0


def seed_watermark(cur: psycopg.Cursor, last_event_id: int) -> None:
    cur.execute(
        """
        INSERT INTO pipeline_watermarks (job_name, last_event_id)
        VALUES (%s, %s)
        ON CONFLICT (job_name)
        DO UPDATE SET
            last_event_id = EXCLUDED.last_event_id,
            updated_at = NOW()
        """,
        (JOB_NAME, last_event_id),
    )


def load_events(cur: psycopg.Cursor, last_event_id: int) -> list[dict]:
    cur.execute(
        """
        SELECT
            event_id,
            movie_id,
            event_type,
            previous_rating,
            new_rating
        FROM rating_events
        WHERE event_id > %s
        ORDER BY event_id
        LIMIT %s
        """,
        (last_event_id, FETCH_BATCH_SIZE),
    )
    return list(cur.fetchall())


def decimal_value(value) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def aggregate_event_deltas(events: list[dict]) -> dict[int, dict[str, Decimal | int]]:
    deltas: dict[int, dict[str, Decimal | int]] = {}
    for event in events:
        movie_id = int(event["movie_id"])
        event_type = event["event_type"]
        previous_rating = event["previous_rating"]
        new_rating = event["new_rating"]

        rating_count_delta = 0
        rating_sum_delta = Decimal("0")

        if event_type == "upsert":
            if previous_rating is None and new_rating is not None:
                rating_count_delta = 1
                rating_sum_delta = decimal_value(new_rating)
            elif previous_rating is not None and new_rating is not None:
                rating_sum_delta = decimal_value(new_rating) - decimal_value(previous_rating)
        elif event_type == "delete" and previous_rating is not None:
            rating_count_delta = -1
            rating_sum_delta = -decimal_value(previous_rating)

        if rating_count_delta == 0 and rating_sum_delta == 0:
            continue

        entry = deltas.setdefault(movie_id, {"rating_count_delta": 0, "rating_sum_delta": Decimal("0")})
        entry["rating_count_delta"] = int(entry["rating_count_delta"]) + rating_count_delta
        entry["rating_sum_delta"] = decimal_value(entry["rating_sum_delta"]) + rating_sum_delta

    return deltas


def apply_deltas(cur: psycopg.Cursor, deltas: dict[int, dict[str, Decimal | int]]) -> None:
    for movie_id, delta in deltas.items():
        rating_count_delta = int(delta["rating_count_delta"])
        rating_sum_delta = decimal_value(delta["rating_sum_delta"])

        cur.execute(
            """
            SELECT rating_count, rating_sum
            FROM movie_rating_stats
            WHERE movie_id = %s
            FOR UPDATE
            """,
            (movie_id,),
        )
        row = cur.fetchone()

        current_count = int(row["rating_count"]) if row else 0
        current_sum = decimal_value(row["rating_sum"]) if row else Decimal("0")
        next_count = current_count + rating_count_delta
        next_sum = current_sum + rating_sum_delta

        if next_count <= 0:
            cur.execute("DELETE FROM movie_rating_stats WHERE movie_id = %s", (movie_id,))
            continue

        next_avg = next_sum / Decimal(next_count)
        if row:
            cur.execute(
                """
                UPDATE movie_rating_stats
                SET
                    rating_count = %s,
                    rating_sum = %s,
                    avg_rating = %s,
                    updated_at = NOW()
                WHERE movie_id = %s
                """,
                (next_count, next_sum, next_avg, movie_id),
            )
        else:
            cur.execute(
                """
                INSERT INTO movie_rating_stats (
                    movie_id,
                    rating_count,
                    rating_sum,
                    avg_rating
                )
                VALUES (%s, %s, %s, %s)
                """,
                (movie_id, next_count, next_sum, next_avg),
            )


def refresh_popular_recall(cur: psycopg.Cursor) -> int:
    cur.execute("TRUNCATE TABLE recall_popular_movies")
    cur.execute(POPULAR_RECALL_FROM_STATS_SQL)
    cur.execute("SELECT COUNT(*) AS cnt FROM recall_popular_movies")
    return int(cur.fetchone()["cnt"])


def process_incremental_events() -> None:
    started_at = time.perf_counter()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM movie_rating_stats")
            stats_row_count = int(cur.fetchone()["cnt"])
            if stats_row_count == 0:
                seeded_rows = rebuild_movie_rating_stats(cur)
                print(f"[rating_events] seeded movie_rating_stats rows={seeded_rows}")

            last_event_id = get_last_event_id(cur)
            total_events = 0
            batch_count = 0

            while True:
                events = load_events(cur, last_event_id)
                if not events:
                    break

                batch_count += 1
                deltas = aggregate_event_deltas(events)
                if deltas:
                    apply_deltas(cur, deltas)

                last_event_id = int(events[-1]["event_id"])
                seed_watermark(cur, last_event_id)
                total_events += len(events)
                print(
                    f"[rating_events] batch={batch_count} "
                    f"events={len(events)} touched_movies={len(deltas)} last_event_id={last_event_id}"
                )

            popular_count = refresh_popular_recall(cur)
        conn.commit()

    elapsed = time.perf_counter() - started_at
    print(
        f"[rating_events] completed total_events={total_events} "
        f"popular_rows={popular_count} elapsed={elapsed:.2f}s"
    )


if __name__ == "__main__":
    process_incremental_events()
