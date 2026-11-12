"""Every agent-facing string lives here so it can be reviewed in one place."""
from datetime import date, timedelta

from isuite import house_terms

NEED_INVITE = (f"Hi! iSuite is for {house_terms.AGENCY_NAME} agents. "
               if house_terms.AGENCY_NAME else "Hi! This iSuite is invite only. ") + (
               "Ask your leader for an invite code, then send /start <code>.")
CHECK_CONSENT = "Check consent first"
PHOTO_UNREADABLE = "I couldn't read that photo. Could you type the names and numbers instead?"
FILE_TOO_BIG = ("That file is too big for me to read. "
                "Please send it as a normal photo instead.")
FILE_NOT_PHOTO = ("I can only read photos. Please send a photo, forward a contact, "
                  "or type: lead: Name 91234567")
AI_DOWN = "I can't read leads right now. Please try again shortly."
OPEN_EDITION_NOTE = ("You can carry on with the free open edition of iSuite, "
                     "which you run yourself.")
PAUSED_NO_DATE = ("Your iSuite is paused. Ask your leader if you would like to come back. "
                  + OPEN_EDITION_NOTE)


PAUSED_DELETED = ("Your iSuite is paused and your contacts have been deleted. "
                  "Ask your leader if you would like to come back. " + OPEN_EDITION_NOTE)


PAUSED_DELETING = ("Your iSuite is paused. Your contacts are being deleted. "
                   "Ask your leader if you would like to come back. " + OPEN_EDITION_NOTE)


def paused(last_kept_on: str | None) -> str:
    """last_kept_on: ISO date (YYYY-MM-DD), the last day the agent's contacts are kept, or None."""
    try:
        d = date.fromisoformat(last_kept_on)
    except (TypeError, ValueError):
        return PAUSED_NO_DATE
    return (f"Your iSuite is paused. Your contacts will be deleted after {d.day} {d:%b %Y}, "
            "so please send /export to take a copy before then. " + OPEN_EDITION_NOTE)
SORRY = "Sorry, something went wrong on my side. Please try again in a moment."
ADMIN_ONLY = "That's for admins only."
NOT_FOUND = "I couldn't find that contact."
NOTHING_PENDING = "Nothing waiting to be saved."
NO_CONTACTS = "No contacts yet."
CANCELLED = "Okay, nothing was saved."
PENDING_EXPIRED = ("That list is more than a day old, so I did not save it. "
                   "Please send those leads again.")
NO_LEADS_TEXT = "I couldn't find a lead in that. Please resend it as: lead: Name 91234567"
LEAD_USAGE = "Please send it like this: lead: Name 91234567"
LEFT_USAGE = "Please send it like this: /left <telegram id>"
REJOIN_USAGE = "Please send it like this: /rejoin <telegram id>"
NO_AGENTS = "No agents yet."
LEFT_UNKNOWN = "No agent with that Telegram id."
ADMIN_NO_AGENT = "You are set up as an admin only. Send /start <code> to join as an agent too."
CONSENT_KEPT_NO = ("This person said no to being contacted, so I've kept it that way. "
                   "They won't appear in your daily list.")
ALREADY_NOTED = "Already noted. I'll remind you when the next follow-up is due."
BTN_OK = "Looks right ✓"
BTN_CANCEL = "Cancel"

WELCOME = (
    "Welcome to iSuite! I help you keep track of the people you meet and remind you "
    "who to follow up with.\n\n"
    "Three things to know:\n"
    "- Your leader sees your numbers only, never your clients.\n"
    "- You are responsible for the client details you enter here.\n"
    "- iSuite never sends messages for you. I only write drafts, and you send them yourself.\n\n"
    "To add someone, send a photo of a form or name card, forward a contact, "
    "or type: lead: Name 91234567"
)

SOLO_WELCOME = (
    "Welcome to iSuite! I help you keep track of the people you meet and remind you "
    "who to follow up with.\n\n"
    "Three things to know:\n"
    "- This copy is only for you. Your contacts are saved on the computer that runs me, "
    "not on anyone else's server. The photos and text you send also pass through Telegram "
    "and are processed by an outside AI service.\n"
    "- You are responsible for the client details you enter here.\n"
    "- iSuite never sends messages for you. I only write drafts, and you send them yourself.\n\n"
    "To add someone, send a photo of a form or name card, forward a contact, "
    "or type: lead: Name 91234567"
)
SOLO_PRIVATE = "This is a private copy of iSuite, so it only works for its owner."
SOLO_TEAM_ONLY = ("That is for the team edition. This copy is just for you, "
                  "so there are no invites or team views.")

HELP = (
    "Here is what you can send me:\n"
    "- A photo of a form or name card\n"
    "- A forwarded contact\n"
    "- lead: Name 91234567\n"
    "- roadshow <where> to mark a roadshow day\n"
    "- /today, /week, /export"
)

HELP_UNKNOWN = (
    "Sorry, I did not understand that.\n\n"
    "To add someone by typing, start the message with the word lead and a colon, "
    "like this: lead: Name 91234567\n\n" + HELP
)

_CONSENT = {"yes": "consent ✓", "unknown": "consent ?", "no": "no consent"}
_OUTCOME = {
    "messaged": "Noted: you messaged them.",
    "replied": "Noted: they replied.",
    "booked": "Noted: appointment booked.",
    "closed": "Noted: case closed.",
    "not_keen": "Noted: not keen for now.",
}


def consent_label(consent: str) -> str:
    label = _CONSENT.get(consent, _CONSENT["unknown"])
    if consent not in ("yes", "no"):
        label += f" ({CHECK_CONSENT})"
    return label


NO_PHONE = "no phone (if you sent a number, tap Cancel and type it again)"


def pending_summary(leads: list[dict]) -> str:
    lines = ["I found these. Save them?"]
    for i, lead in enumerate(leads, 1):
        name = lead.get("name") or "(no name)"
        phone = lead.get("phone_raw") or NO_PHONE
        lines.append(f"{i}. {name}, {phone}, {consent_label(lead.get('consent', 'unknown'))}")
    return "\n".join(lines)


def saved(total: int, merged: int) -> str:
    noun = "lead" if total == 1 else "leads"
    return f"Saved {total} {noun} ({merged} merged)"


def consent_button(name: str) -> str:
    return f"Consent given: {name or 'contact'}"


def consent_saved() -> str:
    return "Noted: consent given."


def outcome_saved(outcome: str) -> str:
    return _OUTCOME[outcome]


def roadshow_on(where: str) -> str:
    return f"Roadshow day logged{(' at ' + where) if where else ''}. Good luck out there!"


def invite_code(code: str) -> str:
    return f"New invite code: {code}"


def left_done(name: str | None, told: bool = True) -> str:
    who = name or "That agent"
    base = (f"Done. {who} has left the team. Team extras stopped now. "
            "They have 30 days to export, then their contacts are deleted")
    if told:
        return base + ", and they have been told."
    return (base + f". {who} could not be reached on Telegram, so please tell them yourself "
            "and give them the last day their data is kept.")


def left_not_on_team(name: str | None, status: str) -> str:
    return f"{name or 'That agent'} is not on the team right now (status: {status}). Nothing changed."


def rejoin_done(name: str | None) -> str:
    return f"Done. {name or 'That agent'} is back on the team."


def rejoin_already(name: str | None) -> str:
    return f"{name or 'That agent'} is already on the team."


def agent_line(name: str | None, telegram_id: int, status: str, paused_on: str | None = None) -> str:
    tail = ""
    if status == "paused":
        try:
            tail = f", data kept until {date.fromisoformat(paused_on) + timedelta(days=30)}"
        except (TypeError, ValueError):
            pass
    return f"{name or '(no name)'}, {telegram_id}, {status}{tail}"


NOTHING_TODAY = "Nobody to message today. Enjoy the quiet, or add a new lead!"
CHECK_NUMBER = "Check number"
NO_TEAM = "No team members yet."
ROADSHOW_NUDGE = "You did a roadshow yesterday and logged 0 leads. Anyone you spoke to?"
BTN_WHATSAPP = "Open WhatsApp"
BTN_SENT = "Sent"
BTN_REPLIED = "Replied"
BTN_BOOKED = "Booked"
BTN_NOT_KEEN = "Not keen"
BTN_HAS_CONSENT = "Has consent ✓"


def greeting(hour: int) -> str:
    """hour: the SGT hour, 0-23."""
    if hour < 12:
        return "Good morning"
    return "Good afternoon" if hour < 18 else "Good evening"


def brief_header(name: str | None, count: int, hour: int) -> str:
    who = "1 person" if count == 1 else f"{count} people"
    return f"{greeting(hour)} {name or 'there'}! {who} to message today."


def brief_more(count: int) -> str:
    return f"+{count} more. Send /today to see them later."


def brief_contact(name: str | None, stage: str, sent_count: int, body: str) -> str:
    step = "first message" if not sent_count else f"follow-up {sent_count}"
    return f"{name or '(no name)'} ({stage}, {step})\n{body}"


def _stats_line(s: dict) -> str:
    return (f"{s['leads_captured']} leads, {s['messaged']} messaged, "
            f"{s['followed_up_48h']}% followed up within 48h, {s['replied']} replied, "
            f"{s['booked']} booked, {s['closed']} closed, {s['roadshows']} roadshows")


def week_summary(stats: dict) -> str:
    return "Your week so far: " + _stats_line(stats)


def team_summary(rows: list[dict]) -> str:
    return "Team this week:\n" + "\n".join(f"{r['name'] or '(no name)'}: {_stats_line(r)}" for r in rows)
