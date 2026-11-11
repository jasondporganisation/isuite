import sys
from datetime import date
from typing import TYPE_CHECKING

from isuite import wording
from isuite.drafts import DraftBudget, draft_with_link
from isuite.contacts import due_today, messages_sent
from isuite.followup import SGT

if TYPE_CHECKING:
    from isuite.bot import Deps
    from isuite.roster import Agent

MAX_LISTED = 15


def _outcome_row(cid: int) -> list[tuple[str, str]]:
    return [(wording.BTN_SENT, f"out:{cid}:messaged"), (wording.BTN_REPLIED, f"out:{cid}:replied"),
            (wording.BTN_BOOKED, f"out:{cid}:booked"), (wording.BTN_NOT_KEEN, f"out:{cid}:not_keen")]


def _contact_message(deps: "Deps", agent: "Agent", row,
                     budget: DraftBudget | None = None) -> tuple[str, list]:
    buttons: list[list[tuple[str, str]]] = []
    sent = messages_sent(deps.conn, agent.id, row["id"])
    if row["consent"] != "yes":
        body = wording.CHECK_CONSENT
        if row["consent"] == "unknown":
            buttons.append([(wording.BTN_HAS_CONSENT, f"consent:{row['id']}")])
    elif not (row["phone_ok"] and row["phone"]):
        body = wording.CHECK_NUMBER
    else:
        text, link = draft_with_link(deps.llm, row, agent.name or "", sent, budget)
        if link:
            body = text
            buttons.append([(wording.BTN_WHATSAPP, link)])
        else:
            body = wording.CHECK_NUMBER
    buttons.append(_outcome_row(row["id"]))
    return wording.brief_contact(row["name"], row["stage"], sent, body), buttons


def send_brief(deps: "Deps", agent: "Agent", today: date,
               budget: DraftBudget | None = None) -> int:
    if budget is None:
        budget = DraftBudget()
    rows = due_today(deps.conn, agent.id, today)
    if not rows:
        deps.tg.send(agent.telegram_id, wording.NOTHING_TODAY)
        return 0
    deps.tg.send(agent.telegram_id, wording.brief_header(
        agent.name, len(rows), deps.now().astimezone(SGT).hour))
    listed = rows[:MAX_LISTED]
    delivered = 0
    for row in listed:
        try:
            text, buttons = _contact_message(deps, agent, row, budget)
            deps.tg.send(agent.telegram_id, text, buttons)
            delivered += 1
        except Exception as e:
            # One bad contact must not end the brief. Type name only: no name or phone.
            print(f"isuite brief error: {type(e).__name__}", file=sys.stderr)
    if len(rows) > len(listed):
        deps.tg.send(agent.telegram_id, wording.brief_more(len(rows) - len(listed)))
    return delivered
