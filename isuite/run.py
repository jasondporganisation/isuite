import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from isuite import bot, schedule
from isuite.bot import Deps, handle_update
from isuite.config import Config, load_config, load_env_file
from isuite.db import connect
from isuite.llm import ClaudeClient, FakeLLM
from isuite.telegram_api import BotAPI, FakeTelegram, TelegramError

RETRY_SECONDS = 5
POLL_SECONDS = 30
ALBUM_POLL_SECONDS = 2  # short poll while an album summary is waiting to go out


def _report(exc: Exception) -> None:
    # Type name only: messages can carry the bot token or client details.
    print(f"isuite loop error: {type(exc).__name__}", file=sys.stderr)


HINT_TOKEN = ("Telegram did not accept the bot token. Stop iSuite, run "
              "python3 -m isuite.setup again with the token from BotFather, then start it again.")
HINT_CLASH = ("Another program is using this bot, so messages can go missing. Stop the other "
              "program, or make a new bot for iSuite with BotFather and run setup again. "
              "Run iSuite in one window, on one computer only.")
HINT_OFFLINE = ("Could not reach Telegram. Check the internet connection. "
                "iSuite will keep trying.")
_HINT_BY_CODE = {0: HINT_OFFLINE, 401: HINT_TOKEN, 404: HINT_TOKEN, 409: HINT_CLASH}
_hints_shown: set[str] = set()


def reset_hints() -> None:
    _hints_shown.clear()


def hint_once(exc: Exception) -> None:
    """One plain line, once per run, for a polling failure a person can fix. Chosen from the
    error's number only, never from its text."""
    if not isinstance(exc, TelegramError):
        return
    hint = _HINT_BY_CODE.get(exc.code)
    if hint and hint not in _hints_shown:
        _hints_shown.add(hint)
        print(f"isuite: {hint}", file=sys.stderr)


def step(deps: Deps, offset: int | None, sleep: Callable[[float], None] = time.sleep) -> int | None:
    """Poll once, handle each update, run due jobs. Returns the next offset."""
    try:
        try:
            waiting = bot.summaries_waiting(deps)
        except Exception as e:
            _report(e)
            waiting = False
        updates = deps.tg.get_updates(offset, timeout=ALBUM_POLL_SECONDS if waiting else POLL_SECONDS)
    except Exception as e:
        _report(e)
        hint_once(e)
        sleep(RETRY_SECONDS)
        return offset
    for update in updates:
        uid = update.get("update_id")
        if isinstance(uid, int):
            offset = max(offset or 0, uid + 1)
            try:  # stored before handling, so a restart never replays a poison update
                schedule.store_offset(deps.conn, offset)
            except Exception as e:
                _report(e)
        handle_update(deps, update)
    try:  # one summary per photo album, after the whole batch is handled
        bot.flush_summaries(deps)
    except Exception as e:
        _report(e)
    now = deps.now()
    try:
        for name in schedule.due_jobs(now, schedule.load_last_run(deps.conn)):
            try:
                schedule.run_job(deps, name, now)
            except Exception as e:
                _report(e)
                try:  # drop any half-finished write so a later commit cannot keep it
                    deps.conn.rollback()
                except Exception as e2:
                    _report(e2)
    except Exception as e:
        _report(e)
    return offset


def _missing(cfg: Config) -> str | None:
    if not cfg.telegram_token:
        return "ISUITE_TELEGRAM_TOKEN"
    if not cfg.anthropic_key:
        return "ANTHROPIC_API_KEY"
    return None


# ---------- dry run ----------

def _msg(uid: int, text: str | None = None, **extra) -> dict:
    m = {"from": {"id": uid, "first_name": "Sam"}, "chat": {"id": uid, "type": "private"}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"message": m}


def _cb(uid: int, data: str) -> dict:
    return {"callback_query": {"id": "cb", "from": {"id": uid}, "data": data,
                               "message": {"chat": {"id": uid, "type": "private"}}}}


def _show(tg: FakeTelegram, start: int) -> int:
    for m in tg.sent[start:]:
        print(f"[to {m['chat_id']}] {m['text']}")
        for row in m["buttons"] or []:
            print("    buttons: " + " | ".join(label for label, _ in row))
    return len(tg.sent)


def dry_run() -> int:
    admin, agent = 1, 100
    conn = connect(":memory:")
    try:
        tg = FakeTelegram()
        leads = json.dumps({"leads": [
            {"name": "Tan", "phone": "9123 4567", "consent_ticked": True, "notes": ""},
            {"name": "Lim", "phone": "81234567", "consent_ticked": None, "notes": ""}]})
        drafts = ["Hi Tan, Sam here. Lovely chatting at the roadshow! "
                  "Happy to share more whenever it suits you.",
                  "Hi Tan, Sam again. Hope your week is going well!"]
        llm = FakeLLM([leads] + drafts)
        clock = [datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)]  # 10:00 SGT
        deps = Deps(conn, Config("dry", "dry", ":memory:", {admin}), tg, llm, lambda: clock[0])
        tg.files["photo1"] = b"fake-image"
        shown = 0

        def say(label: str) -> None:
            print(f"--- {label}")

        def feed(*updates: dict) -> None:
            nonlocal shown
            tg.queue.extend(updates)
            step(deps, None)
            shown = _show(tg, shown)

        say("Day 1, 10:00 SGT: admin invites, agent joins")
        feed(_msg(admin, "/invite"))
        code = tg.sent[-1]["text"].split()[-1]
        feed(_msg(agent, f"/start {code}"))
        say("Agent starts a roadshow and photographs a signup sheet")
        feed(_msg(agent, "roadshow Sat Tampines"), _msg(agent, photo=[{"file_id": "photo1"}]))
        feed(_cb(agent, "ok"))
        say("Day 2, 08:30 SGT: morning brief")
        clock[0] = datetime(2026, 11, 11, 0, 30, tzinfo=timezone.utc)
        feed()
        say("Agent taps Messaged on the first lead")
        cid = conn.execute("SELECT id FROM contacts WHERE name='Tan'").fetchone()["id"]
        feed(_cb(agent, f"out:{cid}:messaged"))
        say("Day 5, 08:30 SGT: follow-up comes due")
        clock[0] = datetime(2026, 11, 14, 0, 30, tzinfo=timezone.utc)
        feed(_msg(admin, "/team"))
    finally:
        conn.close()
    return 0


# ---------- entry point ----------

def effective_env(env: Mapping[str, str], config_file: Path | None) -> Mapping[str, str]:
    """The environment, or the private config file when there is no other config at all.

    The file is read only if a config_file is given, it exists, and the environment has no
    ISUITE_* variable and no ANTHROPIC_API_KEY (a blank one counts as set). If any of those is
    present the file is ignored completely, with no per-key merge, so a team set-up
    configured by its supervisor can never turn solo by accident. A .env in the working
    directory is never read."""
    if config_file is None or not Path(config_file).exists():
        return env
    if any(k.startswith("ISUITE_") or k == "ANTHROPIC_API_KEY" for k in env):
        return env
    return {**env, **load_env_file(Path(config_file))}


def default_config_file() -> Path:
    return Path.home() / ".isuite" / "config.env"


def main(argv: list[str] | None = None, env: Mapping[str, str] = os.environ,
         config_file: Path | None = None,
         serve: Callable[[Config], int] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="isuite.run")
    parser.add_argument("--dry-run", action="store_true",
                        help="run a scripted day against fakes; no network, no env vars")
    args = parser.parse_args(argv)
    if args.dry_run:
        return dry_run()
    try:
        cfg = load_config(effective_env(env, config_file))
    except ValueError as e:  # the message names the variable, never its value
        print(f"isuite: {e}", file=sys.stderr)
        return 2
    missing = _missing(cfg)
    if missing:
        print(f"isuite: {missing} is not set; refusing to start", file=sys.stderr)
        return 2
    return (serve or _serve)(cfg)


RUNNING = ("iSuite is running. Leave this window open and message your bot in Telegram. "
           "To stop it, click this window and press Control and C.")
STOPPED = "iSuite has stopped. Your contacts are saved. Start it again the same way."


def _serve(cfg: Config, out: Callable[[str], None] = print) -> int:
    """The real polling loop. Tests never reach it: they pass their own `serve` to main()."""
    conn = connect(cfg.db_path)
    deps = Deps(conn, cfg, BotAPI(cfg.telegram_token), ClaudeClient(cfg.anthropic_key),
                lambda: datetime.now(timezone.utc))
    offset = schedule.load_offset(conn)
    reset_hints()
    out(RUNNING)
    try:
        while True:
            offset = step(deps, offset)
    except KeyboardInterrupt:
        out(STOPPED)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(config_file=default_config_file()))
