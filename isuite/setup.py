"""Easy start for the open edition: python3 -m isuite.setup

Asks for the Telegram bot token, your Telegram user id and the Anthropic API key, and writes
them to ~/.isuite/config.env (folder mode 700, file mode 600). The two secrets are typed with
the screen hidden. Nothing is echoed, printed or logged.

If you do not know your Telegram id, setup shows a number to send to your new bot. Leave the
answer empty and setup asks Telegram (and only Telegram) who sent that number, and uses that
person's id. That is the one time setup uses the internet."""
import getpass
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Callable

from isuite.telegram_api import BotAPI, TelegramError

MAX_TRIES = 3
MAX_LOOKUPS = 6
NAME_LIMIT = 40


def default_config_dir() -> Path:
    return Path.home() / ".isuite"


_TOKEN_SHAPE = re.compile(r"[0-9]{3,20}:[A-Za-z0-9_-]{20,100}")
_KEY_SHAPE = re.compile(r"[\x21-\x7e]{8,400}")  # one piece of plain visible characters


def _ask_secret(label: str, getpass_fn, out, shape: re.Pattern, hint: str) -> str | None:
    for _ in range(MAX_TRIES):
        value = getpass_fn(f"{label} (hidden as you type): ").strip()
        if shape.fullmatch(value):
            return value
        out(f"That does not look right. {hint} Try again.")
    return None


def _safe_name(name) -> str:
    """A Telegram name is someone else's text: keep plain printable characters, and not many."""
    text = "".join(c for c in str(name or "") if c.isprintable())
    return text[:NAME_LIMIT].strip() or "someone"


def new_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


def recent_senders(token: str, api_factory=BotAPI) -> list[tuple[int, str, str]]:
    """(id, first name, message text) for each private message from a person that is still
    waiting for this bot, oldest first. Reads only; the messages stay queued, so the bot
    still answers them when it starts."""
    found: list[tuple[int, str, str]] = []
    for update in api_factory(token).get_updates(None, timeout=0):
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            continue
        chat, sender, text = message.get("chat"), message.get("from"), message.get("text")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            continue
        uid = sender.get("id")
        if (chat.get("type") == "private" and not sender.get("is_bot")
                and isinstance(uid, int) and not isinstance(uid, bool) and uid > 0):
            found.append((uid, _safe_name(sender.get("first_name")),
                          text if isinstance(text, str) else ""))
    return found


def _owner_prompt(code: str) -> str:
    return ("Your Telegram user id (numbers only). Don't know it? Open your new bot in "
            f"Telegram, send it this number: {code}  then press Enter here: ")


def _look_up_owner(token: str, code: str, out, find_senders) -> str | None:
    """The id of the one person who sent this run's code to the bot. A code only this screen
    shows means a stranger who found the bot cannot be picked as its owner."""
    try:
        senders = find_senders(token)
    except Exception:  # never shown: the text is not ours to trust and may hold the token
        out("Could not check Telegram. Check the bot token and your internet, "
            "or type your id if you know it.")
        return None
    matches = {uid: name for uid, name, text in senders if text.strip() == code}
    if not matches:
        out(f"I have not seen {code} yet. Open your new bot in Telegram, send it {code}, "
            "then press Enter here.")
        return None
    if len(matches) > 1:
        out("More than one person sent that number, so I will not guess. "
            "Type your id yourself.")
        return None
    uid, name = next(iter(matches.items()))
    out(f"Found {_safe_name(name)}, id {uid}.")
    return str(uid)


def _ask_owner(token: str, code: str, input_fn, out, find_senders) -> str | None:
    bad = lookups = 0
    while bad < MAX_TRIES and lookups < MAX_LOOKUPS:
        value = input_fn(_owner_prompt(code)).strip()
        if not value:
            lookups += 1
            found = _look_up_owner(token, code, out, find_senders)
            if found:
                return found
            continue
        if value.isascii() and value.isdigit() and int(value) > 0:
            return value
        bad += 1
        out("That should be a number, like 123456789. Try again.")
    return None


class SetupError(Exception):
    """A plain-words reason setup could not save the file. Never carries a secret."""


def _clear_stale_temp(tmp: Path) -> None:
    """A temp file left by an earlier crash is removed if it is a plain file that is ours.
    Anything else (a symlink, a folder, someone else's file) is refused, never written through."""
    try:
        info = os.lstat(tmp)
    except FileNotFoundError:
        return
    ours = not hasattr(os, "getuid") or info.st_uid == os.getuid()
    if not stat.S_ISREG(info.st_mode) or not ours:
        raise SetupError(f"{tmp} is in the way and is not a plain file of yours. "
                         "Look at it, remove it, then run setup again.")
    try:
        os.unlink(tmp)
    except OSError:
        raise SetupError(f"{tmp} is in the way and could not be removed. "
                         "Remove it, then run setup again.") from None


def _write_private(path: Path, text: str) -> None:
    """Write via a new file that is private from the first byte, then swap it in."""
    tmp = path.with_name(path.name + ".tmp")
    _clear_stale_temp(tmp)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None, *, config_dir: Path,
         input_fn: Callable[[str], str] = input,
         getpass_fn: Callable[[str], str] = getpass.getpass,
         out: Callable[[str], None] = print,
         find_senders: Callable[[str], list] = recent_senders,
         code_fn: Callable[[], str] = new_code) -> int:
    config_dir = Path(config_dir)
    path = config_dir / "config.env"
    try:
        if path.exists():
            answer = input_fn(f"{path} already exists. Replace it? (y/n): ").strip().lower()
            if answer not in ("y", "yes"):
                out("Okay, nothing changed.")
                return 0
        out("Three questions. Your answers are saved only on this computer.")
        token = _ask_secret("Telegram bot token from BotFather", getpass_fn, out, _TOKEN_SHAPE,
                            "It is a number, a colon, then a long mix of letters, with no spaces.")
        owner = (_ask_owner(token, code_fn(), input_fn, out, find_senders) if token else None)
        key = (_ask_secret("Anthropic API key", getpass_fn, out, _KEY_SHAPE,
                           "It should be one piece with no spaces.") if owner else None)
    except (KeyboardInterrupt, EOFError):
        print("isuite setup: stopped, nothing was saved", file=sys.stderr)
        return 1
    if not (token and owner and key):
        print("isuite setup: no valid answer, nothing was saved", file=sys.stderr)
        return 1
    text = (f"ISUITE_TELEGRAM_TOKEN={token}\nANTHROPIC_API_KEY={key}\n"
            f"ISUITE_OWNER={owner}\nISUITE_DB={config_dir / 'isuite.db'}\n")
    try:
        config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(config_dir, 0o700)
        _write_private(path, text)
    except SetupError as e:
        print(f"isuite setup: {e} Nothing was saved.", file=sys.stderr)
        return 1
    except OSError as e:  # the error text can hold a path, never a value; keep to the type
        print(f"isuite setup: could not save the file ({type(e).__name__}). Nothing was saved.",
              file=sys.stderr)
        return 1
    out(f"Saved to {path} (only you can read it).")
    out("Next, start the bot: python3 -m isuite.run")
    return 0


if __name__ == "__main__":
    sys.exit(main(config_dir=default_config_dir()))
