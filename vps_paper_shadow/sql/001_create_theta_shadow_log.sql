-- Theta-inversion paper-shadow log.
--
-- One row per processed signal_snapshots entry. Stores all three composite
-- hypotheses (H0 original / H1 drop-theta / H2 invert-theta) plus the
-- sub-scores so the data is self-contained for analysis.
--
-- Idempotent: snapshot_id is UNIQUE so re-runs of the harness are safe.

CREATE TABLE IF NOT EXISTS theta_shadow_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id        INTEGER NOT NULL UNIQUE,    -- ref to signal_snapshots.id
    market_id          TEXT NOT NULL,
    snapshot_ts        TEXT NOT NULL,              -- ISO datetime
    processed_at       REAL NOT NULL,              -- unix timestamp of this insert
    edge_score         REAL NOT NULL,
    volume_score       REAL NOT NULL,
    whale_score        REAL NOT NULL,
    theta_score        REAL NOT NULL,
    h0_confidence      REAL NOT NULL,              -- original composite
    h1_confidence      REAL NOT NULL,              -- drop theta, renormalize
    h2_confidence      REAL NOT NULL,              -- invert theta (100 - theta)
    confirmations      INTEGER NOT NULL DEFAULT 0,
    category           TEXT,
    days_to_close      REAL,
    price_at_signal    REAL,
    raw_json_truncated TEXT                        -- first 200 chars for debug
);

CREATE INDEX IF NOT EXISTS idx_tsl_market_ts
    ON theta_shadow_log(market_id, snapshot_ts);

CREATE INDEX IF NOT EXISTS idx_tsl_processed_at
    ON theta_shadow_log(processed_at);
