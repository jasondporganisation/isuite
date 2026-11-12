import json
import re

from isuite.contacts import LeadDraft
from isuite.llm import HAIKU, LLM
from isuite.redact import has_nric, redact

SYSTEM = (
    "You read insurance roadshow lead forms, name cards and notes. Return only JSON: "
    "{\"leads\":[{\"name\":string,\"phone\":string,\"consent_ticked\":true|false|null,"
    "\"notes\":string}]}. Never include NRIC, FIN or policy numbers. "
    "If nothing is readable return {\"leads\":[]}."
)

READ_TIMEOUT = 45  # seconds: a dead AI blocks the loop for at most this long per photo

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


class ExtractError(Exception):
    pass


def _consent(v) -> str:
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return "unknown"


_RUN = re.compile(r"[0-9][0-9 ()+\-]*")


def _digit_runs(text: str) -> list[str]:
    """The digits of each number as it was typed: '9123 4567, Lim 8123-4567' gives
    ['91234567', '81234567']. Keeping the runs apart stops a number with an extra digit
    from passing because that digit starts the next number."""
    return [re.sub(r"\D", "", m.group(0)) for m in _RUN.finditer(text)]


def _phone_in_source(phone: str, runs: list[str]) -> bool:
    """True when the phone's digits appear unbroken inside one typed number.
    A leading 65 the AI added (or dropped) is allowed."""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return True
    if any(digits in run for run in runs):
        return True
    return (digits.startswith("65") and len(digits) > 8
            and any(digits[2:] in run for run in runs))


def extract_leads(llm: LLM, *, text: str | None = None,
                  image: bytes | None = None,
                  image_type: str = "image/jpeg") -> list[LeadDraft]:
    user = redact(text) if text else "Extract the leads from the attached image."
    raw = llm.complete(SYSTEM, user, model=HAIKU, image=image, image_type=image_type,
                       max_tokens=4096, timeout=READ_TIMEOUT, retries=0)
    raw = raw.strip()
    m = _FENCE.match(raw)
    if m:
        raw = m.group(1)
    try:
        data = json.loads(raw)
    except ValueError:
        raise ExtractError("Reply was not valid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("leads"), list):
        raise ExtractError("Reply JSON has no 'leads' list")
    leads = []
    runs = _digit_runs(text or "")
    for item in data["leads"]:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip() if isinstance(item.get("name"), str) else ""
        phone = item.get("phone")
        phone = str(phone).strip() if phone is not None else ""
        if has_nric(phone):
            phone = ""
        if text and phone and not _phone_in_source(phone, runs):
            # The AI returned a number that was not typed. A wrong number means a
            # message to a stranger, so drop it and let the agent type it again.
            phone = ""
        if not name and not phone:
            continue
        notes = item.get("notes")
        notes = notes if isinstance(notes, str) else ""
        leads.append(LeadDraft(name=redact(name), phone_raw=phone,
                               consent=_consent(item.get("consent_ticked")),
                               notes=redact(notes.strip())))
    return leads
