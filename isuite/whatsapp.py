import urllib.parse


def wa_link(phone_e164: str, text: str) -> str:
    digits = "".join(ch for ch in phone_e164 if ch.isdigit())
    return f"https://wa.me/{digits}?text=" + urllib.parse.quote(text, safe="")
