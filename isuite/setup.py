"""Easy start for the open edition: python3 -m isuite.setup

Asks for the Telegram bot token, your Telegram user id and the Anthropic API key, and writes
them to ~/.isuite/config.env (folder mode 700, file mode 600). The two secrets are typed with
the screen hidden. Nothing is echoed, printed or logged, and nothing is sent anywhere."""
import getpass
import os
import re
import stat
import sys
from pathlib import Path
from typing import Callable

MAX_TRIES = 3
_NO_SPACE = re.compile(r"\S+")


def default_config_dir() -> Path:
    return Path.home() / ".isuite"


def _ask_secret(label: str, getpass_fn, out) -> str | None:
    for _ in range(MAX_TRIES):
        value = getpass_fn(f"{label} (hidden as you type): ").strip()
        if _NO_SPACE.fullmatch(value):  # one word: no spaces, no line breaks
            return value
        out("That does not look right. It should be one piece with no spaces. Try again.")
    return None


def _ask_owner(input_fn, out) -> str | None:
    for _ in range(MAX_TRIES):
        value = input_fn("Your Telegram user id (numbers only): ").strip()
        if value.isascii() and value.isdigit() and int(value) > 0:
            return value
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
         out: Callable[[str], None] = print) -> int:
    config_dir = Path(config_dir)
    path = config_dir / "config.env"
    try:
        if path.exists():
            answer = input_fn(f"{path} already exists. Replace it? (y/n): ").strip().lower()
            if answer not in ("y", "yes"):
                out("Okay, nothing changed.")
                return 0
        out("Three questions. Your answers are saved only on this computer.")
        token = _ask_secret("Telegram bot token from BotFather", getpass_fn, out)
        owner = _ask_owner(input_fn, out) if token else None
        key = _ask_secret("Anthropic API key", getpass_fn, out) if owner else None
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
