-- 내전·참가·티어 원본이 정리되어도 Discord 메시지 정리를 재시도할 수 있다.
CREATE TABLE IF NOT EXISTS cancellation_jobs (
    match_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    notice_sent INTEGER NOT NULL DEFAULT 0 CHECK (notice_sent IN (0, 1))
);
