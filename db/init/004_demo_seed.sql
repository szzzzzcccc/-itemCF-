COPY movies (
    movie_id,
    movielens_title,
    movielens_genres,
    tmdb_id,
    imdb_id,
    media_type,
    tmdb_source,
    tmdb_title,
    original_title,
    overview,
    poster_path,
    poster_url,
    backdrop_path,
    release_date,
    runtime,
    vote_average,
    vote_count,
    popularity,
    original_language,
    tmdb_genres,
    adult
)
FROM '/workspace/demo_data/movies.csv'
WITH (FORMAT csv, HEADER true);

COPY ratings (
    user_id,
    movie_id,
    rating,
    rating_timestamp
)
FROM '/workspace/demo_data/ratings_demo_users.csv'
WITH (FORMAT csv, HEADER true);

COPY movie_rating_stats (
    movie_id,
    rating_count,
    rating_sum,
    avg_rating,
    updated_at
)
FROM '/workspace/demo_data/movie_rating_stats.csv'
WITH (FORMAT csv, HEADER true);

COPY recall_popular_movies (
    movie_id,
    recall_score,
    rank_no,
    rating_count,
    avg_rating,
    updated_at
)
FROM '/workspace/demo_data/recall_popular_movies.csv'
WITH (FORMAT csv, HEADER true);

COPY recall_itemcf_similarity (
    source_movie_id,
    target_movie_id,
    sim_score,
    co_like_users,
    source_like_users,
    target_like_users,
    updated_at
)
FROM '/workspace/demo_data/recall_itemcf_similarity.csv'
WITH (FORMAT csv, HEADER true);

COPY recall_twotower_user_embeddings (
    user_id,
    embedding_json,
    vector_norm,
    positive_count,
    updated_at
)
FROM '/workspace/demo_data/recall_twotower_user_embeddings_demo.csv'
WITH (FORMAT csv, HEADER true);

COPY recall_twotower_movie_embeddings (
    movie_id,
    embedding_json,
    vector_norm,
    item_bias,
    positive_user_count,
    updated_at
)
FROM '/workspace/demo_data/recall_twotower_movie_embeddings.csv'
WITH (FORMAT csv, HEADER true);
