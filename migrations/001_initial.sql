PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS match_sessions (
    id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    manager_user_id INTEGER NOT NULL,
    command_channel_id INTEGER NOT NULL,
    announcement_channel_id INTEGER NOT NULL,
    announcement_message_id INTEGER UNIQUE,
    tier_channel_id INTEGER NOT NULL,
    admin_channel_id INTEGER NOT NULL,
    mode TEXT,
    participant_limit INTEGER NOT NULL CHECK (participant_limit > 0),
    status TEXT NOT NULL CHECK (status IN ('CREATED', 'RECRUITING', 'FULL', 'STARTED', 'CANCELED')),
    starts_at_utc TEXT NOT NULL,
    tier_deadline_at_utc TEXT NOT NULL,
    lobby_at_utc TEXT NOT NULL,
    full_reached_at_utc TEXT,
    recruitment_completed_notified_at_utc TEXT,
    tier_complete_notified_at_utc TEXT,
    tier_missing_reminder_notified_at_utc TEXT,
    lobby_notified_at_utc TEXT,
    last_missing_tier_reminder_at_utc TEXT,
    next_arrival_seq INTEGER NOT NULL DEFAULT 1,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_session_per_guild
ON match_sessions(guild_id)
WHERE status IN ('CREATED', 'RECRUITING', 'FULL');

CREATE TABLE IF NOT EXISTS roster_entries (
    match_id TEXT NOT NULL REFERENCES match_sessions(id) ON DELETE CASCADE,
    discord_user_id INTEGER NOT NULL,
    discord_display_name TEXT NOT NULL,
    reaction_order INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('CONFIRMED', 'WAITLISTED', 'WITHDRAWN')),
    reacted_at_utc TEXT NOT NULL,
    removed_at_utc TEXT,
    PRIMARY KEY (match_id, discord_user_id)
);

CREATE INDEX IF NOT EXISTS ix_roster_match_status_order
ON roster_entries(match_id, status, reaction_order);

CREATE TABLE IF NOT EXISTS reaction_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL REFERENCES match_sessions(id) ON DELETE CASCADE,
    discord_user_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('ADD', 'REMOVE')),
    arrival_seq INTEGER NOT NULL,
    received_at_utc TEXT NOT NULL,
    outcome TEXT NOT NULL,
    UNIQUE (match_id, arrival_seq)
);

CREATE TABLE IF NOT EXISTS tier_submissions (
    match_id TEXT NOT NULL REFERENCES match_sessions(id) ON DELETE CASCADE,
    discord_user_id INTEGER NOT NULL,
    discord_display_name TEXT NOT NULL,
    raw_tier_message TEXT NOT NULL,
    discord_message_id INTEGER NOT NULL,
    activity_at_utc TEXT NOT NULL,
    collected_at_utc TEXT NOT NULL,
    PRIMARY KEY (match_id, discord_user_id),
    UNIQUE (match_id, discord_message_id)
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL REFERENCES match_sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('RECRUITMENT_COMPLETE', 'TIER_MISSING_REMINDER', 'LOBBY_REMINDER', 'TIER_COMPLETE')),
    channel_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'FAILED', 'CANCELED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_utc TEXT NOT NULL,
    last_error TEXT,
    discord_message_id INTEGER,
    created_at_utc TEXT NOT NULL,
    sent_at_utc TEXT
);

CREATE INDEX IF NOT EXISTS ix_notification_pending
ON notification_outbox(status, next_attempt_at_utc);
