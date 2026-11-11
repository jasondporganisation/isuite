import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from isuite.followup import CHASE_WINDOW_DAYS, SGT, next_due
from isuite.phone import normalize_sg
from isuite.redact import redact

OUTCOMES = ("messaged", "replied", "booked", "closed", "not_keen")


@dataclass
class LeadDraft:
    name: str
    phone_raw: str
    consent: str
    notes: str = ""


def _log(conn, agent_id: int, contact_id: int, kind: str, now: datetime,
         detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO activity (agent_id, contact_id, kind, at, detail) VALUES (?,?,?,?,?)",
        (agent_id, contact_id, kind, now.astimezone(timezone.utc).isoformat(), detail),
    )


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def add_or_merge(conn: sqlite3.Connection, agent_id: int, lead: LeadDraft,
                 source: str, today: date, now: datetime) -> tuple[int, bool]:
    """today: SGT date; now: the caller's current time (aware, from deps.now())."""
    phone, ok = normalize_sg(lead.phone_raw)
    name = redact((lead.name or "").strip())
    notes = redact((lead.notes or "").strip())
    existing = None
    if phone:
        existing = conn.execute(
            "SELECT * FROM contacts WHERE agent_id=? AND phone=?", (agent_id, phone)
        ).fetchone()
    if existing:
        new_name = existing["name"] or name
        old_notes = existing["notes"] or ""
        if notes and notes not in old_notes:
            new_notes = f"{old_notes}; {notes}" if old_notes else notes
        else:
            new_notes = old_notes
        consent = existing["consent"]
        if lead.consent == "no":
            consent = "no"
        elif consent == "unknown" and lead.consent == "yes":
            consent = "yes"
        conn.execute(
            "UPDATE contacts SET name=?, notes=?, consent=? WHERE id=? AND agent_id=?",
            (new_name, new_notes, consent, existing["id"], agent_id),
        )
        if existing["stage"] == "stale":  # captured again after the chase ended: chase afresh
            conn.execute(
                "UPDATE contacts SET stage='new', captured_on=?, chase_step=0, next_due_on=?"
                " WHERE id=? AND agent_id=?",
                (_iso(today), _iso(next_due(today, 0)), existing["id"], agent_id))
            # A date cannot tell a "Sent" tap before this re-capture from one after it on the
            # same day, so the restart is marked in the activity log (messages_sent reads it).
            _log(conn, agent_id, existing["id"], "restarted", now, source)
        _log(conn, agent_id, existing["id"], "merged", now, source)
        conn.commit()
        return existing["id"], False
    cur = conn.execute(
        "INSERT INTO contacts (agent_id, name, phone, phone_ok, source, consent, stage,"
        " captured_on, next_due_on, chase_step, notes) VALUES (?,?,?,?,?,?,'new',?,?,0,?)",
        (agent_id, name, phone or None, 1 if ok else 0, source, lead.consent,
         _iso(today), _iso(next_due(today, 0)), notes or None),
    )
    _log(conn, agent_id, cur.lastrowid, "captured", now, source)
    conn.commit()
    return cur.lastrowid, True


def _next_chase(captured_on: date, step: int, today: date) -> tuple[int, date | None]:
    """The step after `step` whose chase day is strictly after today, skipping passed ones."""
    nxt = step + 1
    while True:
        due = next_due(captured_on, nxt)
        if due is None or due > today:
            return nxt, due
        nxt += 1


def record_outcome(conn: sqlite3.Connection, agent_id: int, contact_id: int,
                   outcome: str, today: date, now: datetime) -> bool:
    """Returns False when a "Sent" tap is ignored because this step is already noted."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome}")
    row = conn.execute(
        "SELECT * FROM contacts WHERE id=? AND agent_id=?", (contact_id, agent_id)
    ).fetchone()
    if row is None:
        raise KeyError(contact_id)
    if outcome == "messaged" and row["stage"] not in ("new", "messaged"):
        pass  # terminal or stale: log only, never resume the chase
    elif outcome == "messaged":
        if row["next_due_on"] and date.fromisoformat(row["next_due_on"]) > today:
            return False  # double tap: the next follow-up is already set
        step, due = _next_chase(date.fromisoformat(row["captured_on"]), row["chase_step"], today)
        stage = "messaged" if due else "stale"
        conn.execute(
            "UPDATE contacts SET chase_step=?, stage=?, next_due_on=? WHERE id=? AND agent_id=?",
            (step, stage, _iso(due), contact_id, agent_id),
        )
    else:
        conn.execute(
            "UPDATE contacts SET stage=?, next_due_on=NULL WHERE id=? AND agent_id=?",
            (outcome, contact_id, agent_id),
        )
    _log(conn, agent_id, contact_id, outcome, now, today.isoformat())
    conn.commit()
    return True


def set_consent(conn: sqlite3.Connection, agent_id: int, contact_id: int, consent: str) -> str:
    """Record consent only where none is recorded yet; a stored 'yes' or 'no' never changes.
    Returns the consent stored after the call."""
    conn.execute(
        "UPDATE contacts SET consent=? WHERE id=? AND agent_id=? AND consent='unknown'",
        (consent, contact_id, agent_id))
    row = conn.execute(
        "SELECT consent FROM contacts WHERE id=? AND agent_id=?", (contact_id, agent_id)
    ).fetchone()
    if row is None:
        raise KeyError(contact_id)
    conn.commit()
    return row["consent"]


def messages_sent(conn: sqlite3.Connection, agent_id: int, contact_id: int) -> int:
    """How many times this agent has tapped Sent for this contact since it was last captured
    (a lead captured again after going stale starts again from the first message, even when
    the old Sent tap was on the same day: the "restarted" log row marks the point)."""
    row = conn.execute("SELECT captured_on FROM contacts WHERE id=? AND agent_id=?",
                       (contact_id, agent_id)).fetchone()
    if row is None:
        return 0
    since = row["captured_on"]
    mark = conn.execute(
        "SELECT MAX(id) FROM activity WHERE agent_id=? AND contact_id=? AND kind='restarted'",
        (agent_id, contact_id)).fetchone()[0] or 0
    return sum(
        1 for r in conn.execute(
            "SELECT at FROM activity WHERE agent_id=? AND contact_id=? AND kind='messaged'"
            " AND id>?", (agent_id, contact_id, mark))
        if datetime.fromisoformat(r["at"]).astimezone(SGT).date().isoformat() >= since)


def _oldest_live_capture(today: date) -> str:
    """Earliest captured_on still inside the chase window (day 14 itself is inside)."""
    return (today - timedelta(days=CHASE_WINDOW_DAYS)).isoformat()


def due_today(conn: sqlite3.Connection, agent_id: int, today: date) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contacts WHERE agent_id=? AND next_due_on IS NOT NULL AND next_due_on<=?"
        " AND stage IN ('new','messaged') AND consent!='no' AND captured_on>=?"
        " ORDER BY (consent='yes') DESC, captured_on DESC, id ASC",
        (agent_id, today.isoformat(), _oldest_live_capture(today)),
    ).fetchall()


def expire_stale(conn: sqlite3.Connection, today: date) -> int:
    """Mark every agent's untouched leads past the chase window as stale. Returns the count."""
    cur = conn.execute(
        "UPDATE contacts SET stage='stale', next_due_on=NULL"
        " WHERE stage IN ('new','messaged') AND captured_on<?", (_oldest_live_capture(today),))
    conn.commit()
    return cur.rowcount


PENDING_HOURS = 24  # an unconfirmed waiting list is dropped this long after its last addition


def pending_expired(created_at: str | None, now: datetime) -> bool:
    """True when a waiting list is more than PENDING_HOURS old. A bad stamp counts as expired."""
    try:
        made = datetime.fromisoformat(created_at)
        return now - made > timedelta(hours=PENDING_HOURS)
    except (TypeError, ValueError):
        return True


def purge_expired_pending(conn: sqlite3.Connection, now: datetime) -> int:
    """Delete every expired waiting list (all agents). Returns how many were removed."""
    gone = [r["agent_id"] for r in conn.execute("SELECT agent_id, created_at FROM pending")
            if pending_expired(r["created_at"], now)]
    for agent_id in gone:
        conn.execute("DELETE FROM pending WHERE agent_id=?", (agent_id,))
    conn.commit()
    return len(gone)
