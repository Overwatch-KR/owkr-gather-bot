BEGIN IMMEDIATE;

INSERT OR IGNORE INTO tier_message_bindings (
    discord_message_id, match_id, discord_user_id, bound_at_utc
)
SELECT
    discord_message_id, match_id, discord_user_id, collected_at_utc
FROM tier_submissions;

COMMIT;
