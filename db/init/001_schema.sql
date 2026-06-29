CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS movies (
    movie_id            INTEGER PRIMARY KEY,
    movielens_title     TEXT NOT NULL,
    movielens_genres    TEXT,
    tmdb_id             INTEGER,
    imdb_id             TEXT,
    media_type          TEXT NOT NULL DEFAULT 'movie',
    tmdb_source         TEXT,
    tmdb_title          TEXT,
    original_title      TEXT,
    overview            TEXT,
    poster_path         TEXT,
    poster_url          TEXT,
    backdrop_path       TEXT,
    release_date        DATE,
    runtime             INTEGER,
    vote_average        NUMERIC(6,3),
    vote_count          INTEGER,
    popularity          NUMERIC(12,4),
    original_language   TEXT,
    tmdb_genres         TEXT,
    adult               BOOLEAN
);

CREATE TABLE IF NOT EXISTS ratings (
    user_id             INTEGER NOT NULL,
    movie_id            INTEGER NOT NULL REFERENCES movies(movie_id),
    rating              NUMERIC(2,1) NOT NULL,
    rating_timestamp    BIGINT NOT NULL,
    PRIMARY KEY (user_id, movie_id)
);

CREATE TABLE IF NOT EXISTS rating_events (
    event_id             BIGSERIAL PRIMARY KEY,
    user_id              INTEGER NOT NULL,
    movie_id             INTEGER NOT NULL REFERENCES movies(movie_id),
    event_type           TEXT NOT NULL,
    previous_rating      NUMERIC(2,1),
    new_rating           NUMERIC(2,1),
    event_ts             BIGINT NOT NULL,
    created_at           TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_rating_events_type
        CHECK (event_type IN ('upsert', 'delete'))
);

CREATE TABLE IF NOT EXISTS pipeline_watermarks (
    job_name             TEXT PRIMARY KEY,
    last_event_id        BIGINT NOT NULL DEFAULT 0,
    updated_at           TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS movie_rating_stats (
    movie_id             INTEGER PRIMARY KEY REFERENCES movies(movie_id),
    rating_count         INTEGER NOT NULL DEFAULT 0,
    rating_sum           NUMERIC(12,3) NOT NULL DEFAULT 0,
    avg_rating           NUMERIC(6,3),
    updated_at           TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app_users (
    app_user_id         SERIAL PRIMARY KEY,
    username            TEXT NOT NULL UNIQUE,
    display_name        TEXT NOT NULL,
    password_hash       TEXT NOT NULL,
    movielens_user_id   INTEGER NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS recall_popular_movies (
    movie_id            INTEGER PRIMARY KEY REFERENCES movies(movie_id),
    recall_score        NUMERIC(12,6) NOT NULL,
    rank_no             INTEGER NOT NULL,
    rating_count        INTEGER NOT NULL,
    avg_rating          NUMERIC(4,3),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS recall_itemcf_similarity (
    source_movie_id     INTEGER NOT NULL REFERENCES movies(movie_id),
    target_movie_id     INTEGER NOT NULL REFERENCES movies(movie_id),
    sim_score           NUMERIC(12,6) NOT NULL,
    co_like_users       INTEGER NOT NULL,
    source_like_users   INTEGER NOT NULL,
    target_like_users   INTEGER NOT NULL,
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_movie_id, target_movie_id)
);

CREATE TABLE IF NOT EXISTS recall_twotower_user_embeddings (
    user_id             INTEGER PRIMARY KEY,
    embedding_json      TEXT NOT NULL,
    vector_norm         NUMERIC(12,6) NOT NULL,
    positive_count      INTEGER NOT NULL,
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS recall_twotower_movie_embeddings (
    movie_id            INTEGER PRIMARY KEY REFERENCES movies(movie_id),
    embedding_json      TEXT NOT NULL,
    vector_norm         NUMERIC(12,6) NOT NULL,
    item_bias           NUMERIC(12,6) NOT NULL DEFAULT 0,
    positive_user_count INTEGER NOT NULL,
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS staging_movies_raw (
    movie_id            TEXT,
    movielens_title     TEXT,
    movielens_genres    TEXT,
    tmdb_id             TEXT,
    imdb_id             TEXT,
    media_type          TEXT,
    tmdb_source         TEXT,
    tmdb_title          TEXT,
    original_title      TEXT,
    overview            TEXT,
    poster_path         TEXT,
    poster_url          TEXT,
    backdrop_path       TEXT,
    release_date        TEXT,
    runtime             TEXT,
    vote_average        TEXT,
    vote_count          TEXT,
    popularity          TEXT,
    original_language   TEXT,
    tmdb_genres         TEXT,
    adult               TEXT
);

CREATE TABLE IF NOT EXISTS staging_ratings_raw (
    user_id             TEXT,
    movie_id            TEXT,
    rating              TEXT,
    rating_timestamp    TEXT
);

CREATE INDEX IF NOT EXISTS idx_movies_tmdb_title_trgm
    ON movies USING gin (tmdb_title gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_movies_original_title_trgm
    ON movies USING gin (original_title gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_ratings_movie_id
    ON ratings (movie_id);

CREATE INDEX IF NOT EXISTS idx_ratings_user_id
    ON ratings (user_id);

CREATE INDEX IF NOT EXISTS idx_rating_events_event_id
    ON rating_events (event_id);

CREATE INDEX IF NOT EXISTS idx_rating_events_user_id
    ON rating_events (user_id);

CREATE INDEX IF NOT EXISTS idx_rating_events_movie_id
    ON rating_events (movie_id);

CREATE INDEX IF NOT EXISTS idx_recall_popular_rank
    ON recall_popular_movies (rank_no);

CREATE INDEX IF NOT EXISTS idx_recall_itemcf_source
    ON recall_itemcf_similarity (source_movie_id, sim_score DESC);

CREATE INDEX IF NOT EXISTS idx_recall_twotower_movie_positive_users
    ON recall_twotower_movie_embeddings (positive_user_count DESC);
