import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# The only keys the private config file may set; anything else in it is ignored.
FILE_KEYS = ("ISUITE_TELEGRAM_TOKEN", "ANTHROPIC_API_KEY", "ISUITE_OWNER", "ISUITE_DB")


@dataclass
class Config:
    telegram_token: str
    anthropic_key: str
    db_path: str
    admin_ids: set[int]
    tz: str = "Asia/Singapore"
    owner_id: int | None = None  # set: solo mode, this one Telegram user is the whole bot


def _owner(env: Mapping[str, str]) -> int | None:
    raw = env.get("ISUITE_OWNER", "").strip()
    if not raw:
        return None
    if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        # Name the variable, never echo its value.
        raise ValueError("ISUITE_OWNER must be one numeric Telegram user id")
    return int(raw)


def load_config(env: Mapping[str, str] = os.environ) -> Config:
    owner = _owner(env)
    if owner is not None and env.get("ISUITE_ADMINS", "").strip():
        raise ValueError("set ISUITE_OWNER (solo) or ISUITE_ADMINS (team), not both")
    try:
        admins = {int(p) for p in env.get("ISUITE_ADMINS", "").split(",") if p.strip()}
    except ValueError:
        # Name the variable, never echo its value.
        raise ValueError("ISUITE_ADMINS must be comma-separated numeric Telegram ids") from None
    if owner is not None:
        admins = {owner}  # the owner is the one admin
    return Config(
        telegram_token=env.get("ISUITE_TELEGRAM_TOKEN", ""),
        anthropic_key=env.get("ANTHROPIC_API_KEY", ""),
        db_path=env.get("ISUITE_DB") or "isuite.db",
        admin_ids=admins,
        owner_id=owner,
    )


def load_env_file(path: Path) -> dict[str, str]:
    """Read KEY=value lines from the private config file the setup helper writes
    (default ~/.isuite/config.env). A missing file gives {}. A file that other users on the
    computer can read is refused: the message says how to fix it and never shows the contents.
    Blank lines, # comments and keys not in FILE_KEYS are ignored."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return {}
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(f"the config file {path} can be read by other users; "
                         f"run: chmod 600 {path}")
    values: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in FILE_KEYS:
            values[key] = value
    return values
