import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from isuite import brief, contacts, wording, followup, numbers, roster
from isuite.config import Config
from isuite.contacts import LeadDraft
from isuite.extract import ExtractError, extract_leads
from isuite.llm import LLM, LLMError
from isuite.redact import redact
from isuite.telegram_api import Telegram

# "roadshow", then end of text, whitespace, or a colon/dash: "roadshow: Bedok", "Roadshow-Bedok".
_ROADSHOW = re.compile(r"roadshow(?:[ \t]*[:-][ \t]*|\s+|$)", re.IGNORECASE)
PHOTO_FILE_TYPES = ("image/jpeg", "image/png", "image/webp")
MAX_PHOTO_BYTES = 5 * 1024 * 1024
ADMIN_COMMANDS = ("/invite", "/left", "/rejoin", "/agents", "/team")
PAUSED_LIKE = ("paused", "trial")  # 'trial' is a row from before the 90-day trial was retired
OK_BUTTONS = [[(wording.BTN_OK, "ok"), (wording.BTN_CANCEL, "cancel")]]


@dataclass
class Deps:
    conn: sqlite3.Connection
    cfg: Config
    tg: Telegram
    llm: LLM
    now: Callable[[], datetime]


def handle_update(deps: Deps, update: dict) -> None:
    chat_id = None
    try:
        if "callback_query" in update:
            cq = update["callback_query"]
            if not _is_private((cq.get("message") or {}).get("chat")):
                _answer(deps, cq)
                return
            chat_id = (cq.get("message") or {}).get("chat", {}).get("id") or cq["from"]["id"]
            try:
                _handle_callback(deps, cq, chat_id)
            finally:
                _answer(deps, cq)
        elif "message" in update:
            msg = update["message"]
            if not _is_private(msg.get("chat")):
                return
            chat_id = msg.get("chat", {}).get("id") or msg["from"]["id"]
            _handle_message(deps, msg, chat_id)
    except Exception as e:
        print(f"isuite error: {type(e).__name__}", file=sys.stderr)
        try:  # drop any half-finished write so the next handler cannot commit it
            deps.conn.rollback()
        except Exception as e3:
            print(f"isuite error: {type(e3).__name__}", file=sys.stderr)
        if chat_id is not None:
            try:
                deps.tg.send(chat_id, wording.SORRY)
            except Exception as e2:
                print(f"isuite error: {type(e2).__name__}", file=sys.stderr)


def _answer(deps: Deps, cq: dict) -> None:
    # Telegram refuses answers to old taps; the tap's effect is already handled, so carry on.
    try:
        deps.tg.answer_callback(cq["id"])
    except Exception as e:
        print(f"isuite error: {type(e).__name__}", file=sys.stderr)


def _is_private(chat: dict | None) -> bool:
    return (chat or {}).get("type") == "private"


def _today(deps: Deps):
    return followup.sg_today(deps.now())


def _paused_text(deps: Deps, agent) -> str:
    """The paused notice. delete_date is the first day the data may be gone, so the last day it
    is kept is the day before. From the delete date on the notice says "deleted" only when this
    agent has no contact rows left; until the hourly purge has run it says "being deleted", with
    no date. An unreadable pause date gives the plain notice with no date."""
    due = roster.delete_date(agent)
    if due is None:
        return wording.PAUSED_NO_DATE
    if _today(deps) >= due:
        left = deps.conn.execute("SELECT COUNT(*) FROM contacts WHERE agent_id=?",
                                 (agent.id,)).fetchone()[0]
        return wording.PAUSED_DELETING if left else wording.PAUSED_DELETED
    return wording.paused((due - timedelta(days=1)).isoformat())


def _command(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower() if parts else ""
    return cmd, parts[1].strip() if len(parts) > 1 else ""


# ---------- messages ----------

def _handle_message(deps: Deps, msg: dict, chat_id: int) -> None:
    sender = msg["from"]["id"]
    text = (msg.get("text") or "").strip()
    cmd, arg = _command(text) if text.startswith("/") else ("", "")

    if deps.cfg.owner_id is not None:  # solo mode: one owner, no invites, no team commands
        if sender != deps.cfg.owner_id:
            deps.tg.send(chat_id, wording.SOLO_PRIVATE)
            return
        _enrol_owner(deps, msg["from"])
        if cmd == "/start":
            deps.tg.send(chat_id, wording.SOLO_WELCOME)
            return
        if cmd in ADMIN_COMMANDS:
            deps.tg.send(chat_id, wording.SOLO_TEAM_ONLY)
            return

    if cmd == "/start":
        _start(deps, msg, chat_id, sender, arg)
        return

    agent = roster.get_agent(deps.conn, sender)
    admin = roster.is_admin(deps.cfg, sender)
    if agent is None and not admin:
        deps.tg.send(chat_id, wording.NEED_INVITE)
        return
    if cmd in ADMIN_COMMANDS and admin:
        _admin_command(deps, chat_id, sender, cmd, arg)  # works even if the admin's own row is paused
        return
    if agent is not None and agent.status in PAUSED_LIKE:
        if cmd == "/export":
            _export(deps, chat_id, agent)
        else:
            deps.tg.send(chat_id, _paused_text(deps, agent))
        return
    if cmd in ADMIN_COMMANDS:
        deps.tg.send(chat_id, wording.ADMIN_ONLY)
        return

    if agent is None:
        deps.tg.send(chat_id, wording.ADMIN_NO_AGENT)
        return

    if cmd == "/today":
        _cmd_today(deps, agent, chat_id)
    elif cmd == "/week":
        _cmd_week(deps, agent, chat_id)
    elif cmd == "/export":
        _export(deps, chat_id, agent)
    elif msg.get("photo"):
        _photo_message(deps, chat_id, agent, msg["photo"], album=bool(msg.get("media_group_id")))
    elif msg.get("contact"):
        _contact(deps, chat_id, agent, msg["contact"])
    elif msg.get("document"):
        _document(deps, chat_id, agent, msg["document"], album=bool(msg.get("media_group_id")))
    elif text.lower().startswith("lead:"):
        _lead_text(deps, chat_id, agent, text[5:].strip())
    elif (roadshow := _ROADSHOW.match(text)):
        _roadshow(deps, chat_id, agent, text[roadshow.end():].strip())
    elif cmd == "/help":
        deps.tg.send(chat_id, wording.HELP)
    else:
        deps.tg.send(chat_id, wording.HELP_UNKNOWN)


def _enrol_owner(deps: Deps, user: dict) -> None:
    """Solo mode: the owner's row is created on their first message. Existing rows are left alone."""
    if roster.get_agent(deps.conn, user["id"]) is None:
        roster.enrol_owner(deps.conn, user["id"], (user.get("first_name") or "").strip(),
                           _today(deps))


def _admin_command(deps, chat_id, sender, cmd, arg) -> None:
    if cmd == "/invite":
        deps.tg.send(chat_id, wording.invite_code(roster.create_invite(deps.conn, sender, deps.now())))
    elif cmd == "/left":
        _left(deps, chat_id, arg)
    elif cmd == "/rejoin":
        _rejoin(deps, chat_id, arg)
    elif cmd == "/agents":
        _cmd_agents(deps, chat_id)
    else:
        _cmd_team(deps, chat_id)


def _start(deps, msg, chat_id, sender, code) -> None:
    existing = roster.get_agent(deps.conn, sender)
    if existing is not None:
        deps.tg.send(chat_id, _paused_text(deps, existing) if existing.status in PAUSED_LIKE
                     else wording.WELCOME)
        return
    name = (msg["from"].get("first_name") or "").strip()
    agent = roster.redeem_invite(deps.conn, code, sender, name, _today(deps)) if code else None
    deps.tg.send(chat_id, wording.WELCOME if agent else wording.NEED_INVITE)


def _cmd_today(deps: Deps, agent, chat_id: int) -> None:
    brief.send_brief(deps, agent, _today(deps))


def _cmd_week(deps: Deps, agent, chat_id: int) -> None:
    week_start = numbers.week_start_of(_today(deps))
    deps.tg.send(chat_id, wording.week_summary(numbers.agent_week(deps.conn, agent.id, week_start)))


def _cmd_team(deps: Deps, chat_id: int) -> None:
    rows = numbers.team_view(deps.conn, numbers.week_start_of(_today(deps)))
    deps.tg.send(chat_id, wording.team_summary(rows) if rows else wording.NO_TEAM)


def _find_target(deps, chat_id, arg, usage):
    try:
        tg_id = int(arg)
    except ValueError:
        deps.tg.send(chat_id, usage)
        return None
    try:
        target = roster.get_agent(deps.conn, tg_id)
    except OverflowError:
        target = None
    if target is None:
        deps.tg.send(chat_id, wording.LEFT_UNKNOWN)
    return target


def _left(deps, chat_id, arg) -> None:
    target = _find_target(deps, chat_id, arg, wording.LEFT_USAGE)
    if target is None:
        return
    updated, changed = roster.mark_left(deps.conn, target.id, _today(deps))
    told = False
    if changed:  # tell the agent plainly: the last day their data is kept, and the open edition
        try:
            deps.tg.send(target.telegram_id, _paused_text(deps, updated))
            told = True
        except Exception as e:
            print(f"isuite error: {type(e).__name__}", file=sys.stderr)
    deps.tg.send(chat_id, wording.left_done(target.name, told) if changed
                 else wording.left_not_on_team(target.name, updated.status))


def _rejoin(deps, chat_id, arg) -> None:
    target = _find_target(deps, chat_id, arg, wording.REJOIN_USAGE)
    if target is None:
        return
    _, changed = roster.rejoin(deps.conn, target.id)
    deps.tg.send(chat_id, wording.rejoin_done(target.name) if changed
                 else wording.rejoin_already(target.name))


def _cmd_agents(deps, chat_id) -> None:
    agents = roster.list_agents(deps.conn, ("team", "trial", "paused", "left"))
    deps.tg.send(chat_id, "\n".join(
        wording.agent_line(a.name, a.telegram_id, a.status, a.paused_on) for a in agents)
        if agents else wording.NO_AGENTS)


def _export(deps, chat_id, agent) -> None:
    rows = deps.conn.execute(
        "SELECT name, phone, stage FROM contacts WHERE agent_id=? ORDER BY id", (agent.id,)
    ).fetchall()
    if not rows:
        deps.tg.send(chat_id, wording.NO_CONTACTS)
        return
    deps.tg.send(chat_id, "\n".join(
        f"{r['name'] or ''}, {r['phone'] or ''}, {r['stage']}" for r in rows))


def _roadshow(deps, chat_id, agent, where) -> None:
    where = redact(where)
    today = _today(deps)
    deps.conn.execute("UPDATE agents SET roadshow_on=? WHERE id=?", (today.isoformat(), agent.id))
    deps.conn.execute(
        "INSERT INTO activity (agent_id, contact_id, kind, at, detail) VALUES (?,NULL,?,?,?)",
        (agent.id, "roadshow", deps.now().isoformat(), where or None))
    deps.conn.commit()
    deps.tg.send(chat_id, wording.roadshow_on(where))


# ---------- intake ----------

def _stage_pending(deps, chat_id, agent, source: str, leads: list[LeadDraft],
                   defer: bool = False) -> None:
    """Append to the agent's waiting list (one OK saves everything) and re-send it all.
    With defer (a photo from an album) the summary is held back: the payload is flagged
    "unsent" and flush_summaries sends one summary once the batch of updates is handled."""
    current = _read_pending(deps, agent)
    if current and _pending_expired(deps, agent):
        current = None  # a list abandoned for a day is dropped, not revived
    items = list(current["leads"]) if current else []
    if current and current.get("source"):  # rows written before leads carried a source
        items = [dict(i, source=i.get("source") or current["source"]) for i in items]
    items += [{"name": redact(l.name or ""), "phone_raw": l.phone_raw, "consent": l.consent,
               "notes": redact(l.notes or ""), "source": source} for l in leads]
    deps.conn.execute(
        "INSERT OR REPLACE INTO pending (agent_id, payload, created_at) VALUES (?,?,?)",
        (agent.id, json.dumps({"leads": items, "unsent": defer}), deps.now().isoformat()))
    deps.conn.commit()
    if not defer:
        deps.tg.send(chat_id, wording.pending_summary(items), OK_BUTTONS)


ALBUM_SUMMARY_TRIES = 3  # a summary that will not send is tried this many times, then dropped
ALBUM_QUIET_SECONDS = 3  # a flagged list is summarised once no photo has been added for this long


def _unsent_rows(deps: Deps) -> list[tuple[sqlite3.Row, dict]]:
    """Waiting lists flagged "unsent" that belong to an active agent and have not expired."""
    out = []
    rows = deps.conn.execute(
        "SELECT p.agent_id, p.payload, p.created_at, a.telegram_id FROM pending p "
        "JOIN agents a ON a.id=p.agent_id WHERE a.status='team'").fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        if payload.get("unsent") and not contacts.pending_expired(row["created_at"], deps.now()):
            out.append((row, payload))
    return out


def summaries_waiting(deps: Deps) -> bool:
    """True while some album summary has not been sent yet (the loop then polls briefly)."""
    return bool(_unsent_rows(deps))


def flush_summaries(deps: Deps) -> None:
    """Send ONE summary for every waiting list flagged "unsent" (album photos) once its last
    addition is ALBUM_QUIET_SECONDS old, then clear the flag. Private chats only, so the chat
    id is the agent's own Telegram id. If the send fails the flag stays and the next flush
    tries again, at most ALBUM_SUMMARY_TRIES attempts in all (the count is kept in the payload);
    after that the flag is cleared and the list is shown in full with the next lead."""
    for row, payload in _unsent_rows(deps):
        try:
            age = deps.now() - datetime.fromisoformat(row["created_at"])
            if age.total_seconds() < ALBUM_QUIET_SECONDS:
                continue
            try:
                deps.tg.send(row["telegram_id"], wording.pending_summary(payload["leads"]),
                             OK_BUTTONS)
                payload["unsent"] = False
            except Exception as e:
                print(f"isuite error: {type(e).__name__}", file=sys.stderr)
                tries = payload.get("send_tries")
                tries = (tries if _is_int(tries) and tries >= 0 else 0) + 1
                payload["send_tries"] = tries
                if tries >= ALBUM_SUMMARY_TRIES:
                    payload["unsent"] = False
            deps.conn.execute("UPDATE pending SET payload=? WHERE agent_id=?",
                              (json.dumps(payload), row["agent_id"]))
            deps.conn.commit()
        except Exception as e:
            print(f"isuite error: {type(e).__name__}", file=sys.stderr)


def _document(deps, chat_id, agent, doc, album: bool = False) -> None:
    """A photo sent "as a file". Anything that is not a small image gets a short reply."""
    mime = doc.get("mime_type")
    if not isinstance(mime, str) or mime.lower() not in PHOTO_FILE_TYPES or not doc.get("file_id"):
        deps.tg.send(chat_id, wording.FILE_NOT_PHOTO)
        return
    size = doc.get("file_size")
    if not _is_int(size) or size > MAX_PHOTO_BYTES:  # an unknown size is not trusted
        deps.tg.send(chat_id, wording.FILE_TOO_BIG)  # nothing is downloaded
        return
    _photo(deps, chat_id, agent, doc["file_id"], album, mime.lower())


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _photo_message(deps, chat_id, agent, sizes, album: bool = False) -> None:
    """Telegram sends a photo in several sizes, smallest first: read the largest one that is
    within the size limit (a size with no file_size counts as fine)."""
    for size in reversed(sizes if isinstance(sizes, list) else []):
        if not isinstance(size, dict) or not size.get("file_id"):
            continue
        known = size.get("file_size")
        if known is None or (_is_int(known) and known <= MAX_PHOTO_BYTES):
            _photo(deps, chat_id, agent, size["file_id"], album)
            return
    deps.tg.send(chat_id, wording.FILE_TOO_BIG)  # nothing is downloaded


def _photo(deps, chat_id, agent, file_id, album: bool = False,
           image_type: str = "image/jpeg") -> None:
    image = deps.tg.download_file(file_id)
    try:
        leads = extract_leads(deps.llm, image=image, image_type=image_type)
    except LLMError as e:
        print(f"isuite ai error: {type(e).__name__}", file=sys.stderr)
        # 400 / 413: the AI refused the image itself; any other error means the AI is down
        deps.tg.send(chat_id, wording.PHOTO_UNREADABLE if e.status in (400, 413)
                     else wording.AI_DOWN)
        return
    except ExtractError:
        leads = []
    if not leads:
        deps.tg.send(chat_id, wording.PHOTO_UNREADABLE)
        return
    _stage_pending(deps, chat_id, agent, "photo", leads, defer=album)


def _contact(deps, chat_id, agent, contact) -> None:
    name = " ".join(p for p in (contact.get("first_name"), contact.get("last_name")) if p)
    lead = LeadDraft(name=name, phone_raw=contact.get("phone_number") or "", consent="unknown")
    _stage_pending(deps, chat_id, agent, "contact", [lead])


def _lead_text(deps, chat_id, agent, body) -> None:
    if not body:
        deps.tg.send(chat_id, wording.LEAD_USAGE)
        return
    try:
        leads = extract_leads(deps.llm, text=body)
    except LLMError as e:
        print(f"isuite ai error: {type(e).__name__}", file=sys.stderr)
        deps.tg.send(chat_id, wording.AI_DOWN)
        return
    except ExtractError:
        leads = []
    if not leads:
        deps.tg.send(chat_id, wording.NO_LEADS_TEXT)
        return
    _stage_pending(deps, chat_id, agent, "text", leads)


# ---------- callbacks ----------

def _handle_callback(deps: Deps, cq: dict, chat_id: int) -> None:
    if deps.cfg.owner_id is not None:
        if cq["from"]["id"] != deps.cfg.owner_id:
            deps.tg.send(chat_id, wording.SOLO_PRIVATE)
            return
        _enrol_owner(deps, cq["from"])
    agent = roster.get_agent(deps.conn, cq["from"]["id"])
    if agent is None:
        deps.tg.send(chat_id, wording.NEED_INVITE)
        return
    if agent.status in PAUSED_LIKE:
        deps.tg.send(chat_id, _paused_text(deps, agent))
        return
    data = cq.get("data") or ""
    if data == "ok":
        _confirm_pending(deps, chat_id, agent)
        _retire_buttons(deps, cq, chat_id)  # only after the save, so a failed save can be retried
    elif data == "cancel":
        _cancel_pending(deps, chat_id, agent)
        _retire_buttons(deps, cq, chat_id)
    else:
        _contact_callback(deps, chat_id, agent, data)


def _retire_buttons(deps: Deps, cq: dict, chat_id: int) -> None:
    """Take the Looks right / Cancel buttons off the message they were tapped on. A used
    button left on screen gets tapped again. Best effort: Telegram refuses this for old
    messages, and the tap itself is still handled."""
    message_id = (cq.get("message") or {}).get("message_id")
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        return
    try:
        deps.tg.clear_buttons(chat_id, message_id)
    except Exception as e:
        print(f"isuite error: {type(e).__name__}", file=sys.stderr)


def _read_pending(deps, agent) -> dict | None:
    row = deps.conn.execute(
        "SELECT payload FROM pending WHERE agent_id=?", (agent.id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def _pending_expired(deps, agent) -> bool:
    row = deps.conn.execute(
        "SELECT created_at FROM pending WHERE agent_id=?", (agent.id,)).fetchone()
    return row is not None and contacts.pending_expired(row["created_at"], deps.now())


def _clear_pending(deps, agent) -> None:
    deps.conn.execute("DELETE FROM pending WHERE agent_id=?", (agent.id,))
    deps.conn.commit()


def _cancel_pending(deps, chat_id, agent) -> None:
    had = _read_pending(deps, agent) is not None
    _clear_pending(deps, agent)
    deps.tg.send(chat_id, wording.CANCELLED if had else wording.NOTHING_PENDING)


def _confirm_pending(deps, chat_id, agent) -> None:
    payload = _read_pending(deps, agent)
    if payload is None:
        deps.tg.send(chat_id, wording.NOTHING_PENDING)
        return
    if _pending_expired(deps, agent):
        _clear_pending(deps, agent)
        deps.tg.send(chat_id, wording.PENDING_EXPIRED)
        return
    today = _today(deps)
    merged = 0
    ask_consent: list[tuple[str, str]] = []
    for item in payload["leads"]:
        lead = LeadDraft(name=item["name"], phone_raw=item["phone_raw"],
                         consent=item["consent"], notes=item.get("notes", ""))
        contact_id, created = contacts.add_or_merge(
            deps.conn, agent.id, lead, item.get("source") or payload["source"], today,
            deps.now())
        if not created:
            merged += 1
        stored = deps.conn.execute(
            "SELECT name, consent FROM contacts WHERE id=? AND agent_id=?",
            (contact_id, agent.id)).fetchone()
        button = (wording.consent_button(stored["name"]), f"consent:{contact_id}")
        if stored["consent"] == "unknown" and button not in ask_consent:
            ask_consent.append(button)
    _clear_pending(deps, agent)
    buttons = [[b] for b in ask_consent] or None
    deps.tg.send(chat_id, wording.saved(len(payload["leads"]), merged), buttons)


def _contact_callback(deps, chat_id, agent, data) -> None:
    parts = data.split(":")
    try:
        if parts[0] == "out" and len(parts) == 3 and parts[2] in contacts.OUTCOMES:
            recorded = contacts.record_outcome(
                deps.conn, agent.id, int(parts[1]), parts[2], _today(deps), deps.now())
            deps.tg.send(chat_id, wording.outcome_saved(parts[2]) if recorded else wording.ALREADY_NOTED)
        elif parts[0] == "consent" and len(parts) == 2:
            stored = contacts.set_consent(deps.conn, agent.id, int(parts[1]), "yes")
            deps.tg.send(chat_id, wording.CONSENT_KEPT_NO if stored == "no" else wording.consent_saved())
        else:
            deps.tg.send(chat_id, wording.NOT_FOUND)
    except (KeyError, ValueError, OverflowError):
        deps.tg.send(chat_id, wording.NOT_FOUND)
