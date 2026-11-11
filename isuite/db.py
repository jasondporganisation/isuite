import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    telegram_id INTEGER UNIQUE NOT NULL,
    name TEXT,
    status TEXT NOT NULL CHECK(status IN ('team','left','trial','paused')),
    joined_at TEXT,
    left_at TEXT,
    trial_ends_on TEXT,
    roadshow_on TEXT,
    paused_on TEXT
);
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY,
    created_by INTEGER,
    created_at TEXT,
    used_by INTEGER REFERENCES agents(id)
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    name TEXT,
    phone TEXT,
    phone_ok INTEGER NOT NULL DEFAULT 1,
    source TEXT CHECK(source IN ('photo','contact','text')),
    consent TEXT NOT NULL DEFAULT 'unknown' CHECK(consent IN ('yes','unknown','no')),
    stage TEXT NOT NULL DEFAULT 'new'
        CHECK(stage IN ('new','messaged','replied','booked','closed','not_keen','stale')),
    captured_on TEXT NOT NULL,
    next_due_on TEXT,
    chase_step INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    UNIQUE(agent_id, phone)
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    contact_id INTEGER REFERENCES contacts(id),
    kind TEXT NOT NULL,
    at TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS pending (
    agent_id INTEGER PRIMARY KEY REFERENCES agents(id),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    name TEXT PRIMARY KEY,
    last TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    if "paused_on" not in {r["name"] for r in conn.execute("PRAGMA table_info(agents)")}:
        conn.execute("ALTER TABLE agents ADD COLUMN paused_on TEXT")  # database from before 30-day grace
        conn.commit()
    return conn
