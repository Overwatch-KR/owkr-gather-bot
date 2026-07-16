PRAGMA foreign_keys = OFF;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS substitute_recruitments (
    discord_message_id INTEGER PRIMARY KEY,
    match_id TEXT NOT NULL REFERENCES match_sessions(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'FILLED', 'CANCELED')),
    recruited_user_id INTEGER,
    created_at_utc TEXT NOT NULL,
    filled_at_utc TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_substitute_recruitments_one_open
ON substitute_recruitments(match_id)
WHERE status = 'OPEN';

CREATE INDEX IF NOT EXISTS ix_substitute_recruitments_match
ON substitute_recruitments(match_id, status);

CREATE TABLE notification_outbox_v5 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL REFERENCES match_sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN (
        'RECRUITMENT_COMPLETE',
        'SUBSTITUTE_RECRUITED',
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

INSERT INTO notification_outbox_v5 (
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
ALTER TABLE notification_outbox_v5 RENAME TO notification_outbox;

CREATE INDEX ix_notification_pending
ON notification_outbox(status, next_attempt_at_utc);

COMMIT;

PRAGMA foreign_keys = ON;
