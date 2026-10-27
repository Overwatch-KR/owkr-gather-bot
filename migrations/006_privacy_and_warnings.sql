CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('노쇼', '비매너')),
    manager_user_id INTEGER NOT NULL,
    request_id INTEGER NOT NULL UNIQUE,
    created_at_utc TEXT NOT NULL,
    revoked_at_utc TEXT
);
CREATE INDEX IF NOT EXISTS ix_warnings_subject ON warnings(guild_id, user_id);

CREATE TABLE IF NOT EXISTS tier_collection_opt_outs (
    match_id TEXT NOT NULL REFERENCES match_sessions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (match_id, user_id)
);
