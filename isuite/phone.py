import re

_SG_LOCAL = re.compile(r"[3689][0-9]{7}", re.ASCII)


def normalize_sg(raw: str) -> tuple[str, bool]:
    """Return (E.164 number, is_valid_sg). Non-SG input is kept as digits (with a leading + if given)."""
    raw = (raw or "").strip()
    digits = re.sub(r"[^0-9]", "", raw)
    if not digits:
        return "", False
    local = digits[2:] if digits.startswith("65") and len(digits) == 10 else digits
    if _SG_LOCAL.fullmatch(local):
        return "+65" + local, True
    return ("+" if raw.startswith("+") else "") + digits, False
