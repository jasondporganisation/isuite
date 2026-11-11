import sqlite3
from datetime import date, datetime, timedelta

from isuite.followup import SGT

KEYS = ("leads_captured", "messaged", "followed_up_48h", "replied", "booked", "closed", "roadshows")


def week_start_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _sgt_date(iso: str) -> date:
    return datetime.fromisoformat(iso).astimezone(SGT).date()


def agent_week(conn: sqlite3.Connection, agent_id: int, week_start: date) -> dict:
    week_end = week_start + timedelta(days=7)
    counts = {"captured": 0, "messaged": 0, "replied": 0, "booked": 0, "closed": 0, "roadshow": 0}
    first_messaged: dict[int, date] = {}
    rows = conn.execute(
        "SELECT contact_id, kind, at FROM activity WHERE agent_id=? ORDER BY at", (agent_id,)
    ).fetchall()
    for r in rows:
        d = _sgt_date(r["at"])
        if r["kind"] == "messaged" and r["contact_id"] is not None:
            first_messaged.setdefault(r["contact_id"], d)
        if week_start <= d < week_end and r["kind"] in counts:
            counts[r["kind"]] += 1
    captured = conn.execute(
        "SELECT id, captured_on FROM contacts WHERE agent_id=? AND captured_on>=? AND captured_on<?",
        (agent_id, week_start.isoformat(), week_end.isoformat()),
    ).fetchall()
    in_time = sum(
        1 for c in captured
        if c["id"] in first_messaged
        and first_messaged[c["id"]] <= date.fromisoformat(c["captured_on"]) + timedelta(days=2)
    )
    pct = round(100 * in_time / len(captured)) if captured else 0
    return {
        "leads_captured": counts["captured"], "messaged": counts["messaged"],
        "followed_up_48h": pct, "replied": counts["replied"], "booked": counts["booked"],
        "closed": counts["closed"], "roadshows": counts["roadshow"],
    }


def team_view(conn: sqlite3.Connection, week_start: date) -> list[dict]:
    agents = conn.execute(
        "SELECT id, name FROM agents WHERE status='team' ORDER BY id").fetchall()
    return [{"name": a["name"], **agent_week(conn, a["id"], week_start)} for a in agents]
