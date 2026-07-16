PRAGMA foreign_keys = OFF;

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS ux_one_active_session_per_guild;

ALTER TABLE match_sessions ADD COLUMN match_code TEXT;
ALTER TABLE match_sessions ADD COLUMN source_request_id TEXT;
ALTER TABLE match_sessions ADD COLUMN source_request_type TEXT;
ALTER TABLE match_sessions ADD COLUMN tier_anchor_message_id INTEGER;

ALTER TABLE roster_entries ADD COLUMN waitlist_reason TEXT
    CHECK (waitlist_reason IN ('CAPACITY', 'SCHEDULE_CONFLICT'));
ALTER TABLE roster_entries ADD COLUMN conflict_match_id TEXT
    REFERENCES match_sessions(id);

UPDATE roster_entries
SET waitlist_reason = 'CAPACITY'
WHERE status = 'WAITLISTED' AND waitlist_reason IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_match_sessions_match_code
ON match_sessions(match_code)
WHERE match_code IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_match_sessions_source_request
ON match_sessions(source_request_type, source_request_id)
WHERE source_request_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_match_sessions_tier_anchor
ON match_sessions(tier_anchor_message_id)
WHERE tier_anchor_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_match_sessions_active_schedule
ON match_sessions(guild_id, status, starts_at_utc);

CREATE TABLE IF NOT EXISTS tier_message_bindings (
    discord_message_id INTEGER PRIMARY KEY,
    match_id TEXT NOT NULL REFERENCES match_sessions(id) ON DELETE CASCADE,
    discord_user_id INTEGER NOT NULL,
    bound_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_tier_message_bindings_match_user
ON tier_message_bindings(match_id, discord_user_id);

INSERT OR IGNORE INTO tier_message_bindings (
    discord_message_id, match_id, discord_user_id, bound_at_utc
)
SELECT
    discord_message_id, match_id, discord_user_id, collected_at_utc
FROM tier_submissions;

CREATE TABLE notification_outbox_v3 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL REFERENCES match_sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN (
        'RECRUITMENT_COMPLETE',
        'TIER_ANCHOR',
        'TIER_ANCHOR_RECREATED',
        'TIER_MISSING_REMINDER',
        'LOBBY_REMINDER',
        'TIER_COMPLETE'
    )),
    channel_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'SENDING', 'SENT', 'FAILED', 'CANCELED'
    )),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_utc TEXT NOT NULL,
    last_error TEXT,
    discord_message_id INTEGER,
    created_at_utc TEXT NOT NULL,
    sent_at_utc TEXT
);

INSERT INTO notification_outbox_v3 (
    id, match_id, kind, channel_id, payload_json, dedupe_key,
    status, attempts, next_attempt_at_utc, last_error,
    discord_message_id, created_at_utc, sent_at_utc
)
SELECT
    id, match_id, kind, channel_id, payload_json, dedupe_key,
    status, attempts, next_attempt_at_utc, last_error,
    discord_message_id, created_at_utc, sent_at_utc
FROM notification_outbox;

DROP TABLE notification_outbox;
ALTER TABLE notification_outbox_v3 RENAME TO notification_outbox;

CREATE INDEX ix_notification_pending
ON notification_outbox(status, next_attempt_at_utc);

COMMIT;

PRAGMA foreign_keys = ON;
