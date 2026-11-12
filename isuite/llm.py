import base64
import json
import time
import urllib.error
import urllib.request
from typing import Protocol

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
API_URL = "https://api.anthropic.com/v1/messages"


class LLMError(Exception):
    """status: the HTTP code when the API answered with one, otherwise None. The message
    never carries reply text."""

    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status


class LLMCutOff(LLMError):
    """The reply ran out of room (stop_reason max_tokens). The AI itself is working."""


class LLM(Protocol):
    def complete(self, system: str, user: str, *, model: str = HAIKU,
                 image: bytes | None = None, image_type: str = "image/jpeg",
                 max_tokens: int = 1024,
                 timeout: float = 60, retries: int = 1,
                 thinking: bool = True) -> str: ...


class ClaudeClient:
    def __init__(self, api_key: str, sleep=time.sleep):
        self._api_key = api_key
        self._sleep = sleep

    def complete(self, system: str, user: str, *, model: str = HAIKU,
                 image: bytes | None = None, image_type: str = "image/jpeg",
                 max_tokens: int = 1024,
                 timeout: float = 60, retries: int = 1,
                 thinking: bool = True) -> str:
        content = []
        if image is not None:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": image_type,
                "data": base64.b64encode(image).decode("ascii")}})
        content.append({"type": "text", "text": user})
        payload = {
            "model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        if not thinking:
            # The model thinks before it answers unless told not to, and thinking counts
            # against max_tokens. A short job with a small allowance (a draft) can come
            # back with no text at all, so it switches thinking off. Careful reading
            # (names and phone numbers) keeps it on: it is measurably more accurate.
            payload["thinking"] = {"type": "disabled"}
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        for attempt in range(1, retries + 2):
            req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                e.close()
                if attempt <= retries and (e.code == 429 or e.code >= 500):
                    self._sleep(2)
                    continue
                raise LLMError(f"Claude API HTTP {e.code}", status=e.code) from None
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise LLMError(f"Claude API request failed: {type(e).__name__}") from None
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise LLMError("Claude API reply had an unexpected shape")
        if data.get("stop_reason") == "max_tokens":
            # A reply that ran out of room is unfinished. Never pass half a message on.
            raise LLMCutOff("Claude API reply was cut off")
        for block in data["content"]:
            if not isinstance(block, dict):
                raise LLMError("Claude API reply had an unexpected shape")
            if block.get("type") == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise LLMError("Claude API reply had an unexpected shape")
                return text
        raise LLMError("Claude API reply had no text block")


class FakeLLM:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, system: str, user: str, *, model: str = HAIKU,
                 image: bytes | None = None, image_type: str = "image/jpeg",
                 max_tokens: int = 1024,
                 timeout: float = 60, retries: int = 1,
                 thinking: bool = True) -> str:
        self.calls.append({"system": system, "user": user, "model": model,
                           "image": image, "image_type": image_type,
                           "max_tokens": max_tokens,
                           "timeout": timeout, "retries": retries,
                           "thinking": thinking})
        if not self._responses:
            raise LLMError("FakeLLM has no more responses")
        return self._responses.pop(0)
