import re
import sys
import time

from . import house_terms
from .llm import HAIKU, LLMCutOff
from .names import first_name as pick_first_name
from .templates import FALLBACK, FALLBACK_NO_PLACE
from .whatsapp import wa_link

BANNED = ("guarantee", "guaranteed", "risk-free", "no risk", "confirm returns",
          "best plan", "100%", "promise", "risk free", "zero risk", "assured")
# Terms tied to one insurer or agency live in house_terms.py and are added at check time.
_URL_MARKERS = ("http://", "https://", "www.")  # a draft never carries a link
_MET_AT = re.compile(r"\bmet at\s*:?\s+([^;,.]+)", re.IGNORECASE)
_MAX_CHARS = 400
DRAFT_TIMEOUT = 12  # seconds for one AI draft; a slow AI must not freeze the bot


class DraftBudget:
    """Stops AI drafting for the rest of a job once the AI fails or the time is used up.

    Time is a duration from creation, read from `clock`. The default clock is time.monotonic;
    tests inject their own. It is not the calendar date."""

    def __init__(self, seconds: float = 180, clock=time.monotonic):
        self._seconds = seconds
        self._clock = clock
        self._start = clock()
        self._tripped = False

    def trip(self) -> None:
        self._tripped = True

    def allowed(self) -> bool:
        return not self._tripped and self._clock() - self._start < self._seconds

_SYSTEM = (
    "You write one short WhatsApp follow-up message for a Singapore insurance agent "
    "to send to someone they met. Under 60 words. Warm, friendly Singapore English, "
    "no pressure. No product claims, no returns, no promises, no insurer names. "
    "Use only the facts given. Do not invent a place, an event, a date, a meal, "
    "or an earlier chat or meeting. Do not suggest when you met. "
    "Reply with the message text only."
)


def _terms(value) -> tuple[str, ...]:
    """house_terms entries as a clean tuple. A lone string is one term (not its letters);
    None, non-strings and empty or blank entries are skipped; entries are stripped."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    try:
        items = list(value)
    except TypeError:
        return ()
    return tuple(t.strip() for t in items if isinstance(t, str) and t.strip())


def check_copy(text: str) -> list[str]:
    lowered = text.lower()
    found = [term for term in BANNED + _terms(house_terms.EXTRA_BANNED)
             if term.lower() in lowered]
    found += [m for m in _URL_MARKERS if m in lowered]
    found += [w for w in _terms(house_terms.EXTRA_BANNED_WORDS)
              if re.search(rf"\b{re.escape(w)}\b", text)]
    return found


def _fallback(agent_name: str, first_name: str, where: str, sent_count: int) -> str:
    step = min(max(sent_count, 0), 3)
    table = FALLBACK if where else FALLBACK_NO_PLACE
    return table[step].format(first_name=first_name, agent_name=agent_name, where=where)


def make_draft(llm, *, agent_name: str, first_name: str, where: str,
               sent_count: int, notes: str, budget: DraftBudget | None = None) -> str:
    """sent_count = messages the agent has actually sent this contact (picks the wording).
    where = "" means the place is not known; the draft then names no place."""
    where = (where or "").strip()
    if check_copy(where):
        where = ""
    if check_copy(first_name):
        first_name = "there"
    fallback = _fallback(agent_name, first_name, where, sent_count)
    if check_copy(fallback):
        fallback = _fallback("your adviser", first_name, where, sent_count)
    user = (f"Agent name: {agent_name}\nContact first name: {first_name}\n"
            f"Where they met: {where or 'not known, do not mention a place or event'}\n"
            f"Follow-up number: {sent_count}\n"
            f"Notes: {notes or '-'}")
    if budget is not None and not budget.allowed():
        return fallback
    try:
        reply = llm.complete(_SYSTEM, user, model=HAIKU, max_tokens=300,
                             timeout=DRAFT_TIMEOUT, retries=0, thinking=False)
    except LLMCutOff:
        # One over-long reply. The AI is fine, so later drafts in this job still use it.
        print("isuite draft error: LLMCutOff", file=sys.stderr)
        return fallback
    except Exception as e:
        if budget is not None:
            budget.trip()
        # Type name and the HTTP number only, so a bad key or a busy AI shows up
        # without leaking details.
        print(f"isuite draft error: {type(e).__name__}{_status(e)}", file=sys.stderr)
        return fallback
    text = (reply or "").strip().strip("\"'“”").strip()
    if not text or len(text) > _MAX_CHARS or check_copy(text):
        return fallback
    return text


def _status(exc: Exception) -> str:
    status = getattr(exc, "status", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        return f" {status}"
    return ""


def draft_with_link(llm, contact_row, agent_name: str,
                    sent_count: int = 0, budget: DraftBudget | None = None) -> tuple[str, str]:
    name = (contact_row["name"] or "").strip()
    first_name = pick_first_name(name)
    notes = contact_row["notes"] or ""
    m = _MET_AT.search(notes)
    where = m.group(1).strip() if m else ""
    text = make_draft(llm, agent_name=agent_name, first_name=first_name, where=where,
                      sent_count=sent_count, notes=notes, budget=budget)
    phone = contact_row["phone"]
    return text, (wa_link(phone, text) if phone else "")
