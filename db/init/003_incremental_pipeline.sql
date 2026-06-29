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

CREATE INDEX IF NOT EXISTS idx_rating_events_event_id
    ON rating_events (event_id);

CREATE INDEX IF NOT EXISTS idx_rating_events_user_id
    ON rating_events (user_id);

CREATE INDEX IF NOT EXISTS idx_rating_events_movie_id
    ON rating_events (movie_id);
