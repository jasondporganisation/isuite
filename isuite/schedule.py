import sqlite3
import sys
from datetime import datetime, timedelta

from isuite import brief, wording, numbers
from isuite.contacts import expire_stale, purge_expired_pending
from isuite.drafts import DraftBudget
from isuite.followup import SGT, sg_today
from isuite.roster import delete_date, list_agents, purge_paused

JOBS = ("brief", "scorecard", "nudge")
JOB_WAIT_BUDGET = 45  # seconds one job may spend in all waiting out Telegram 429s
BRIEF_UNTIL_HOUR = 12  # the automatic brief is only sent before this SGT hour


def due_jobs(now_utc: datetime, last_run: dict[str, str]) -> list[str]:
    now = now_utc.astimezone(SGT)
    today = now.date().isoformat()
    due = []
    if (8, 30) <= (now.hour, now.minute) < (BRIEF_UNTIL_HOUR, 0) and last_run.get("brief") != today:
        due.append("brief")
    if now.weekday() == 6 and now.hour >= 20 and last_run.get("scorecard") != today:
        due.append("scorecard")
    if last_run.get("nudge") != now.strftime("%Y-%m-%dT%H"):
        due.append("nudge")
    return due


def load_last_run(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["name"]: r["last"] for r in conn.execute("SELECT name, last FROM jobs")}


def _store(conn: sqlite3.Connection, name: str, marker: str) -> None:
    conn.execute("INSERT OR REPLACE INTO jobs(name, last) VALUES (?, ?)", (name, marker))
    conn.commit()


OFFSET_KEY = "tg_offset"  # lives in the jobs table; due_jobs only looks up its own names


def load_offset(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT last FROM jobs WHERE name=?", (OFFSET_KEY,)).fetchone()
        return int(row["last"]) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def store_offset(conn: sqlite3.Connection, offset: int) -> None:
    _store(conn, OFFSET_KEY, str(offset))


def _active_agents(conn):
    return list_agents(conn, ("team",))


def _each(agents, fn) -> None:
    for agent in agents:
        try:
            fn(agent)
        except Exception as e:
            print(f"isuite job error: {type(e).__name__}", file=sys.stderr)


def run_job(deps, name: str, now_utc: datetime) -> None:
    """Store the job marker first, then do the work: a restart or a failed store can never
    repeat sends. If storing raises, nothing is sent and the error goes to the caller."""
    now = now_utc.astimezone(SGT)
    today = sg_today(now_utc)
    conn = deps.conn
    if name not in JOBS:
        raise ValueError(f"unknown job: {name}")
    begin = getattr(deps.tg, "begin_wait_budget", None)
    if begin is not None:
        begin(JOB_WAIT_BUDGET)
    try:
        _run_job(deps, name, now, today, conn)
    finally:
        end = getattr(deps.tg, "end_wait_budget", None)
        if end is not None:
            end()


def _run_job(deps, name: str, now, today, conn) -> None:
    if name == "brief":
        _store(conn, name, today.isoformat())
        budget = DraftBudget()  # one for the whole job: a dead AI is found once, not per agent
        _each(_active_agents(conn), lambda a: brief.send_brief(deps, a, today, budget))
    elif name == "scorecard":
        _store(conn, name, today.isoformat())
        week_start = numbers.week_start_of(today)
        _each(_active_agents(conn), lambda a: deps.tg.send(
            a.telegram_id, wording.week_summary(numbers.agent_week(conn, a.id, week_start))))
    elif name == "nudge":
        _store(conn, name, now.strftime("%Y-%m-%dT%H"))
        _nudge(deps, now)
    else:
        raise ValueError(f"unknown job: {name}")


NUDGE_FROM_HOUR, NUDGE_TO_HOUR = 9, 21


def _nudge(deps, now) -> None:
    conn = deps.conn
    today = now.date()
    def pause_old_trials() -> None:
        # The 90-day trial no longer exists. A row left over from before is a leaver: pause it
        # now and start its 30-day export window today.
        conn.execute(
            "UPDATE agents SET status='paused', paused_on=?, trial_ends_on=NULL "
            "WHERE status='trial'", (today.isoformat(),))
        conn.execute("UPDATE agents SET paused_on=? WHERE status='paused' AND paused_on IS NULL",
                     (today.isoformat(),))  # the 30-day grace starts when the pause is first seen
        for agent in list_agents(conn, ("paused",)):
            if delete_date(agent) is None:  # a hand-edited, unreadable date: start the clock now,
                conn.execute(              # never delete at once and never keep forever
                    "UPDATE agents SET paused_on=? WHERE id=? AND status='paused'",
                    (today.isoformat(), agent.id))
        conn.commit()

    # Each clean-up step stands alone: one that fails is rolled back and logged by type name,
    # and the rest (and the roadshow nudges) still run this hour.
    for step in (pause_old_trials, lambda: purge_paused(conn, today),
                 lambda: expire_stale(conn, today), lambda: purge_expired_pending(conn, now)):
        try:
            step()
        except Exception as e:
            print(f"isuite job error: {type(e).__name__}", file=sys.stderr)
            try:
                conn.rollback()
            except Exception as e2:
                print(f"isuite job error: {type(e2).__name__}", file=sys.stderr)
    if not NUDGE_FROM_HOUR <= now.hour < NUDGE_TO_HOUR:
        return
    yesterday = (today - timedelta(days=1)).isoformat()
    for agent in list_agents(conn, ("paused", "left")):
        if agent.roadshow_on:
            conn.execute("UPDATE agents SET roadshow_on=NULL WHERE id=?", (agent.id,))
    conn.commit()

    def nudge_one(agent) -> None:
        captured_since = [
            r for r in conn.execute(
                "SELECT at FROM activity WHERE agent_id=? AND kind='captured'", (agent.id,))
            if datetime.fromisoformat(r["at"]).astimezone(SGT).date().isoformat()
            >= agent.roadshow_on]
        if not captured_since:
            deps.tg.send(agent.telegram_id, wording.ROADSHOW_NUDGE)
        conn.execute("UPDATE agents SET roadshow_on=NULL WHERE id=?", (agent.id,))
        conn.commit()

    _each([a for a in _active_agents(conn) if a.roadshow_on and a.roadshow_on <= yesterday],
          nudge_one)
