import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Protocol

API_BASE = "https://api.telegram.org"
MAX_MESSAGE = 4096
MAX_RETRY_AFTER = 30  # seconds; a longer wait would freeze the bot, so we give up

Buttons = list[list[tuple[str, str]]]


class TelegramError(Exception):
    """code is Telegram's HTTP or error code when there is one (401, 409, 429 ...), 0 when the
    request got no answer at all, else None.
    It is a plain number, so it is safe to act on where the message text is not."""

    def __init__(self, message: str = "", code: int | None = None):
        super().__init__(message)
        self.code = code


class Telegram(Protocol):
    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]: ...

    def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> None: ...

    def answer_callback(self, callback_id: str, text: str = "") -> None: ...

    def download_file(self, file_id: str) -> bytes: ...

    def begin_wait_budget(self, seconds: float) -> None: ...

    def end_wait_budget(self) -> None: ...


def split_text(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split text into chunks of at most `limit` characters, at line breaks."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current or not chunks:
        chunks.append(current)
    return chunks


def _keyboard(buttons: Buttons) -> dict:
    rows = []
    for row in buttons:
        out = []
        for label, value in row:
            key = "url" if value.startswith("https://") else "callback_data"
            out.append({"text": label, key: value})
        rows.append(out)
    return {"inline_keyboard": rows}


def _retry_after(err: urllib.error.HTTPError) -> float:
    """Seconds Telegram asked us to wait; 1 if the body is missing or unreadable."""
    try:
        data = json.loads(err.read().decode("utf-8"))
        value = data["parameters"]["retry_after"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return 1
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return 1
    return value


class BotAPI:
    def __init__(self, token: str, sleep=time.sleep):
        self._token = token
        self._sleep = sleep
        self._budget: float | None = None  # seconds of 429 waiting left; None = no job running

    def begin_wait_budget(self, seconds: float) -> None:
        """A job (brief, scorecard, nudge) may spend at most this many seconds in all waiting
        out 429s; once it is used up, rate-limited sends fail at once (the job isolates each
        failure). A flood limit then cannot hold a 15-contact brief for minutes."""
        self._budget = float(seconds)

    def end_wait_budget(self) -> None:
        self._budget = None

    def _call(self, method: str, params: dict, http_timeout: int = 30):
        url = f"{API_BASE}/bot{self._token}/{method}"
        body = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"content-type": "application/json"}, method="POST")
        retried = False
        while True:
            try:
                with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                try:
                    code = e.code
                    wait = _retry_after(e) if code == 429 else None
                finally:
                    e.close()
                if (wait is not None and not retried and wait <= MAX_RETRY_AFTER
                        and (self._budget is None or wait <= self._budget)):
                    retried = True
                    if self._budget is not None:
                        self._budget -= wait
                    self._sleep(wait)
                    continue
                raise TelegramError(f"Telegram {method} HTTP {code}", code=code) from None
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
                # code 0: the request never got an answer (no internet, bad URL, cut-off reply)
                raise TelegramError(
                    f"Telegram {method} request failed: {type(e).__name__}", code=0) from None
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description", "unknown error") if isinstance(data, dict) else "bad reply"
            code = data.get("error_code") if isinstance(data, dict) else None
            raise TelegramError(f"Telegram {method} failed: {desc}",
                                code=code if isinstance(code, int) else None)
        return data.get("result")

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        params: dict = {"timeout": timeout,
                        "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params, http_timeout=timeout + 10) or []

    def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> None:
        chunks = split_text(text)
        for i, chunk in enumerate(chunks):
            params: dict = {"chat_id": chat_id, "text": chunk}
            if buttons and i == len(chunks) - 1:
                params["reply_markup"] = _keyboard(buttons)
            self._call("sendMessage", params)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        self._call("answerCallbackQuery", params)

    def download_file(self, file_id: str) -> bytes:
        info = self._call("getFile", {"file_id": file_id})
        path = (info or {}).get("file_path")
        if not path:
            raise TelegramError("Telegram getFile returned no file_path")
        url = f"{API_BASE}/file/bot{self._token}/{path}"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            e.close()
            raise TelegramError(f"Telegram file download HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise TelegramError(
                f"Telegram file download failed: {type(e).__name__}") from None


class FakeTelegram:
    def __init__(self):
        self.sent: list[dict] = []
        self.answered: list[dict] = []
        self.files: dict[str, bytes] = {}
        self.queue: list[dict] = []
        self.budget_calls: list[str] = []
        self.budget_seconds: list[float] = []

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        updates, self.queue = self.queue, []
        return updates

    def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answered.append({"callback_id": callback_id, "text": text})

    def begin_wait_budget(self, seconds: float) -> None:
        self.budget_calls.append("begin")
        self.budget_seconds.append(seconds)

    def end_wait_budget(self) -> None:
        self.budget_calls.append("end")

    def download_file(self, file_id: str) -> bytes:
        if file_id not in self.files:
            raise TelegramError("FakeTelegram has no such file")
        return self.files[file_id]
