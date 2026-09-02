PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '2');

CREATE TABLE IF NOT EXISTS sensor_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL CHECK(timestamp >= 0),
    state_revision INTEGER,
    publication_revision INTEGER NOT NULL UNIQUE,
    system TEXT,
    system_health TEXT,
    mmwave_status TEXT,
    thermal_status TEXT,
    co2_status TEXT,
    pir_status TEXT,
    mmwave_presence INTEGER CHECK(mmwave_presence IN (0, 1) OR mmwave_presence IS NULL),
    respiration_rate_bpm REAL,
    heart_rate_bpm REAL,
    thermal_max_raw INTEGER,
    thermal_max_temp_c REAL,
    thermal_human_probability REAL CHECK(
        thermal_human_probability BETWEEN 0.0 AND 1.0
        OR thermal_human_probability IS NULL
    ),
    thermal_ai_state TEXT,
    co2_ppm REAL,
    pir_motion INTEGER CHECK(pir_motion IN (0, 1) OR pir_motion IS NULL),
    risk_score REAL CHECK(risk_score BETWEEN 0.0 AND 100.0 OR risk_score IS NULL),
    risk_level TEXT CHECK(
        risk_level IN ('NORMAL', 'WARNING', 'DANGER')
        OR risk_level IS NULL
    ),
    is_emergency INTEGER NOT NULL CHECK(is_emergency IN (0, 1)),
    emergency_active INTEGER NOT NULL DEFAULT 0 CHECK(emergency_active IN (0, 1)),
    danger_transition_id TEXT,
    danger_entered_at REAL CHECK(danger_entered_at >= 0 OR danger_entered_at IS NULL),
    alarm_acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(alarm_acknowledged IN (0, 1)),
    alarm_acknowledged_at REAL CHECK(alarm_acknowledged_at >= 0 OR alarm_acknowledged_at IS NULL),
    buzzer_active INTEGER NOT NULL DEFAULT 0 CHECK(buzzer_active IN (0, 1)),
    latched_while_offline INTEGER NOT NULL DEFAULT 0 CHECK(latched_while_offline IN (0, 1)),
    event_type TEXT NOT NULL DEFAULT 'SNAPSHOT',
    risk_reasons_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sensor_snapshots_timestamp
    ON sensor_snapshots(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_sensor_snapshots_risk
    ON sensor_snapshots(risk_level, timestamp DESC);

CREATE TABLE IF NOT EXISTS risk_events (
    event_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL,
    timestamp REAL NOT NULL CHECK(timestamp >= 0),
    event_type TEXT NOT NULL,
    publication_revision INTEGER,
    risk_score REAL CHECK(risk_score BETWEEN 0.0 AND 100.0 OR risk_score IS NULL),
    risk_level TEXT CHECK(
        risk_level IN ('NORMAL', 'WARNING', 'DANGER')
        OR risk_level IS NULL
    ),
    system_health TEXT,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_risk_events_timestamp
    ON risk_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_risk_events_type
    ON risk_events(event_type, timestamp DESC);
