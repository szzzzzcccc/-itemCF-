import math
from collections import defaultdict

import numpy as np


FEATURE_NAMES = [
    "hist_len",
    "avg_year",
    "avg_popularity",
    "avg_vote",
    "movie_year",
    "movie_popularity",
    "movie_vote_average",
    "movie_vote_count_log",
    "movie_genre_count",
    "genre_pref_score",
    "year_gap",
    "popularity_gap",
    "vote_gap",
    "popular_score",
    "popular_rank",
    "genre_score",
    "genre_rank",
    "long_tail_score",
    "long_tail_bonus",
    "long_tail_rank",
    "itemcf_score",
    "itemcf_best_sim",
    "itemcf_support",
    "itemcf_rank",
    "twotower_score",
    "twotower_rank",
    "in_popular",
    "in_genre",
    "in_long_tail",
    "in_itemcf",
    "in_twotower",
    "channel_count",
    "merged_score",
]


def build_user_profile(history_rows: list[dict]):
    if not history_rows:
        return {
            "hist_len": 0.0,
            "avg_year": 2000.0,
            "avg_popularity": 0.0,
            "avg_vote": 0.0,
            "genre_pref": {},
        }

    years = []
    popularities = []
    votes = []
    genre_counter: dict[str, int] = defaultdict(int)
    for row in history_rows:
        years.append(float(row.get("release_year") or 2000.0))
        popularities.append(float(row.get("popularity") or 0.0))
        votes.append(float(row.get("vote_average") or 0.0))
        for genre in row.get("genres") or []:
            genre_counter[genre] += 1

    total_genres = sum(genre_counter.values()) or 1
    genre_pref = {genre: count / total_genres for genre, count in genre_counter.items()}
    return {
        "hist_len": float(len(history_rows)),
        "avg_year": float(np.mean(years)),
        "avg_popularity": float(np.mean(popularities)),
        "avg_vote": float(np.mean(votes)),
        "genre_pref": genre_pref,
    }


def build_lgb_candidate_rows(
    popular_items: list[dict],
    genre_items: list[dict],
    long_tail_items: list[dict],
    itemcf_items: list[dict],
    twotower_items: list[dict],
    merged_candidates: list[dict],
    user_profile: dict,
):
    popular_map = {item["movie_id"]: item for item in popular_items}
    genre_map = {item["movie_id"]: item for item in genre_items}
    long_tail_map = {item["movie_id"]: item for item in long_tail_items}
    itemcf_map = {item["movie_id"]: item for item in itemcf_items}
    twotower_map = {item["movie_id"]: item for item in twotower_items}

    rows = []
    for candidate in merged_candidates:
        movie_id = candidate["movie_id"]
        genres = candidate.get("genres") or []
        genre_pref_score = 0.0
        if genres:
            genre_pref_score = sum(user_profile["genre_pref"].get(genre, 0.0) for genre in genres) / len(genres)

        popular_item = popular_map.get(movie_id)
        genre_item = genre_map.get(movie_id)
        long_tail_item = long_tail_map.get(movie_id)
        itemcf_item = itemcf_map.get(movie_id)
        twotower_item = twotower_map.get(movie_id)

        popular_score = float(popular_item["recall_score"]) if popular_item else 0.0
        popular_rank = float(popular_item.get("rank_no", 999.0)) if popular_item else 999.0
        genre_score = float(genre_item["recall_score"]) if genre_item else 0.0
        genre_rank = float(genre_items.index(genre_item) + 1) if genre_item else 999.0
        long_tail_score = float(long_tail_item["recall_score"]) if long_tail_item else 0.0
        long_tail_bonus = float(long_tail_item.get("long_tail_bonus", 0.0)) if long_tail_item else 0.0
        long_tail_rank = float(long_tail_items.index(long_tail_item) + 1) if long_tail_item else 999.0
        itemcf_score = float(itemcf_item["recall_score"]) if itemcf_item else 0.0
        itemcf_best_sim = float(itemcf_item.get("best_similarity", 0.0)) if itemcf_item else 0.0
        itemcf_support = float(itemcf_item.get("supporting_seed_count", 0.0)) if itemcf_item else 0.0
        itemcf_rank = float(itemcf_items.index(itemcf_item) + 1) if itemcf_item else 999.0
        twotower_score = float(twotower_item["recall_score"]) if twotower_item else -999.0
        twotower_rank = float(twotower_items.index(twotower_item) + 1) if twotower_item else 999.0

        in_popular = 1.0 if popular_item else 0.0
        in_genre = 1.0 if genre_item else 0.0
        in_long_tail = 1.0 if long_tail_item else 0.0
        in_itemcf = 1.0 if itemcf_item else 0.0
        in_twotower = 1.0 if twotower_item else 0.0
        channel_count = in_popular + in_genre + in_long_tail + in_itemcf + in_twotower

        release_year = float(candidate.get("release_year") or 2000.0)
        popularity = float(candidate.get("popularity") or 0.0)
        vote_average = float(candidate.get("vote_average") or 0.0)
        vote_count = float(candidate.get("vote_count") or 0.0)
        merged_score = float(candidate.get("merged_score", 0.0))

        feature_vector = [
            user_profile["hist_len"],
            user_profile["avg_year"],
            user_profile["avg_popularity"],
            user_profile["avg_vote"],
            release_year,
            popularity,
            vote_average,
            math.log1p(vote_count),
            float(len(genres)),
            genre_pref_score,
            abs(release_year - user_profile["avg_year"]),
            abs(popularity - user_profile["avg_popularity"]),
            abs(vote_average - user_profile["avg_vote"]),
            popular_score,
            popular_rank,
            genre_score,
            genre_rank,
            long_tail_score,
            long_tail_bonus,
            long_tail_rank,
            itemcf_score,
            itemcf_best_sim,
            itemcf_support,
            itemcf_rank,
            twotower_score,
            twotower_rank,
            in_popular,
            in_genre,
            in_long_tail,
            in_itemcf,
            in_twotower,
            channel_count,
            merged_score,
        ]
        rows.append(
            {
                "movie_id": movie_id,
                "payload": dict(candidate),
                "features": feature_vector,
            }
        )

    rows.sort(key=lambda row: row["movie_id"])
    return rows
