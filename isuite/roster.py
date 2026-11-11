import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from isuite.config import Config

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
PAUSED_KEEP_DAYS = 30  # a paused agent's data is kept this long after they were paused


@dataclass
class Agent:
    id: int
    telegram_id: int
    name: str | None
    status: str
    joined_at: str | None
    left_at: str | None
    trial_ends_on: str | None
    roadshow_on: str | None
    paused_on: str | None = None


def _to_agent(row: sqlite3.Row | None) -> Agent | None:
    if row is None:
        return None
    return Agent(**{k: row[k] for k in row.keys()})


def get_agent(conn: sqlite3.Connection, telegram_id: int) -> Agent | None:
    row = conn.execute("SELECT * FROM agents WHERE telegram_id=?", (telegram_id,)).fetchone()
    return _to_agent(row)


def list_agents(conn: sqlite3.Connection, statuses: tuple[str, ...]) -> list[Agent]:
    if not statuses:
        return []
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT * FROM agents WHERE status IN ({marks}) ORDER BY id", statuses).fetchall()
    return [_to_agent(r) for r in rows]


def create_invite(conn: sqlite3.Connection, created_by_tg: int, now: datetime) -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if conn.execute("SELECT 1 FROM invites WHERE code=?", (code,)).fetchone() is None:
            break
    conn.execute(
        "INSERT INTO invites(code, created_by, created_at) VALUES (?, ?, ?)",
        (code, created_by_tg, now.astimezone(timezone.utc).isoformat()),
    )
    conn.commit()
    return code


def redeem_invite(conn: sqlite3.Connection, code: str, telegram_id: int,
                  name: str, today: date) -> Agent | None:
    invite = conn.execute("SELECT used_by FROM invites WHERE code=?", (code,)).fetchone()
    if invite is None or invite["used_by"] is not None:
        return None
    existing = get_agent(conn, telegram_id)
    if existing is not None:
        return existing
    cur = conn.execute(
        "INSERT INTO agents(telegram_id, name, status, joined_at) VALUES (?, ?, 'team', ?)",
        (telegram_id, name, today.isoformat()),
    )
    claimed = conn.execute(
        "UPDATE invites SET used_by=? WHERE code=? AND used_by IS NULL",
        (cur.lastrowid, code),
    )
    if claimed.rowcount == 0:
        conn.rollback()
        return None
    conn.commit()
    return get_agent(conn, telegram_id)


def enrol_owner(conn: sqlite3.Connection, telegram_id: int, name: str, today: date) -> Agent:
    """Solo mode: add the owner as a team row with no invite. Safe to call twice."""
    conn.execute(
        "INSERT OR IGNORE INTO agents(telegram_id, name, status, joined_at) VALUES (?, ?, 'team', ?)",
        (telegram_id, name, today.isoformat()))
    conn.commit()
    return get_agent(conn, telegram_id)


def _by_id(conn: sqlite3.Connection, agent_id: int) -> Agent | None:
    return _to_agent(conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())


def mark_left(conn: sqlite3.Connection, agent_id: int, today: date) -> tuple[Agent | None, bool]:
    """A leaver goes straight to 'paused': team extras stop now, and the contacts are kept for
    PAUSED_KEEP_DAYS from today so the agent can export. Only a team member can leave; anyone
    else is left as they are. Returns the agent and whether anything changed."""
    cur = conn.execute(
        "UPDATE agents SET status='paused', left_at=?, paused_on=?, trial_ends_on=NULL "
        "WHERE id=? AND status='team'",
        (today.isoformat(), today.isoformat(), agent_id),
    )
    conn.commit()
    return _by_id(conn, agent_id), cur.rowcount > 0


def rejoin(conn: sqlite3.Connection, agent_id: int) -> tuple[Agent | None, bool]:
    """Put a trial, paused or left agent back on the team. No-op if already on the team."""
    cur = conn.execute(
        "UPDATE agents SET status='team', left_at=NULL, trial_ends_on=NULL, paused_on=NULL "
        "WHERE id=? AND status!='team'", (agent_id,))
    conn.commit()
    return _by_id(conn, agent_id), cur.rowcount > 0


def is_admin(cfg: Config, telegram_id: int) -> bool:
    return telegram_id in cfg.admin_ids


def has_team_extras(agent: Agent) -> bool:
    return agent.status == "team"


def delete_date(agent: Agent) -> date | None:
    """The first day a paused agent's data can be gone (the day after the 30-day grace)."""
    try:
        return date.fromisoformat(agent.paused_on) + timedelta(days=PAUSED_KEEP_DAYS + 1)
    except (TypeError, ValueError):
        return None


def purge_paused(conn: sqlite3.Connection, today: date) -> int:
    """Delete the contacts, activity and waiting list of every paused agent whose pause began
    more than PAUSED_KEEP_DAYS ago. The agent row and invites stay. Only status 'paused' with a
    paused_on date is touched. Returns how many agents lost data."""
    purged = 0
    for agent in list_agents(conn, ("paused",)):
        due = delete_date(agent)
        if due is None or today < due:
            continue
        removed = 0
        for table in ("activity", "contacts", "pending"):  # activity first: it points at contacts
            removed += conn.execute(
                f"DELETE FROM {table} WHERE agent_id=?", (agent.id,)).rowcount
        purged += 1 if removed else 0
    conn.commit()
    return purged
