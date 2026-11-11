import re
import unicodedata

from isuite.phone import normalize_sg

# Whitespace: ASCII plus every Unicode space (the ideographic space U+3000, no-break space ...).
# _HS stays on one line; _WS may cross a line break.
_HS = r"[ \t\f\v\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]"
_WS = r"[ \t\n\r\f\v\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]"
# NRIC/FIN: S1234567D, also written with a space or hyphen inside (S 1234567 D, S1234567-D).
# Look-arounds, not \b: a letter or digit before the first letter blocks a match inside a longer
# word, but digits straight after the last letter do not (S1234567D91234567).
_NRIC = re.compile(
    rf"(?<![A-Z0-9_])[STFGM](?:{_HS}|-){{0,3}}[0-9]{{7}}(?:{_HS}|-){{0,3}}[A-Z](?![A-Z_])",
    re.IGNORECASE | re.ASCII)
# A policy number: one token holding a digit, then any further groups of digits
# ("00 1234 5678" is removed whole), then further numbers of 6+ characters after a comma, a
# semicolon, "&", "/", "+", "and" or "or" ("policy numbers 12345678, 87654321" removes both).
# A further number may carry its own short index ("#2: 87654321").
_TOKEN = r"[A-Z0-9/-]*[0-9][A-Z0-9/-]*"
_MORE = rf"(?:{_HS}+[0-9][0-9/-]*)*"
_CONN = r"(?:and also|as well as|and then|and|or|plus|then|also)"
_SEP = (rf"(?:{_HS}*[,;&/+]{_HS}*(?:{_CONN}\b{_HS}*)?|{_HS}+{_CONN}\b{_HS}+"
        rf"|\.(?=[0-9]{{6}}))")
# A chained number that is a Singapore number with its country code stays (a phone cue "+65").
_NOT_65_PHONE = r"(?!65[689][0-9]{7}(?![0-9A-Z]))"
_NEXT_INDEX = (rf"(?:(?:pol(?:icy)?\.?{_WS}*)?(?:#|nos?\b\.?|num\b\.?){_WS}*[0-9]{{1,2}}\b"
               rf"{_HS}*(?:[:)=]|[.-](?={_HS})){_HS}*)?")
_NEXT = (rf"(?:{_SEP}{_NEXT_INDEX}{_NOT_65_PHONE}(?=[A-Z0-9/-]*[0-9])[A-Z0-9/-]{{6,}}{_MORE})*")
_VALUE = rf"({_TOKEN}{_MORE}{_NEXT})"
# What may sit between a label and its number: spaces, line breaks, . : # = - ( [ ) ] and quotes.
_OPEN = rf"[{_WS[1:-1]}.:#=\-\u2013\u2014\u2212()\[\]\"'\u201c\u201d\u2018\u2019\u00ab\u00bb]*"
# "is" or "are" may come before the value ("policy numbers are 12345678", "policy no (is) 1234").
_IS = rf"(?:(?:is|are)\b{_OPEN})?"
# A short index before the real number: "policy no 1: 12345678", "policy no 1 is 12345678".
# It only counts as an index when a separator follows it, so "policy no 12345678" is unchanged.
# A short list of indexes works too: "policy nos 1 & 2: 12345678", "policy nos. 1,2: 12345678".
_INDEX = (rf"(?:[0-9]{{1,2}}\b(?:{_HS}*(?:[,&]|and\b){_HS}*[0-9]{{1,2}}\b)*{_HS}*"
          rf"(?:[:)=]|[.-](?={_HS})|\.(?=[0-9]{{6}})|(?:is|are)\b){_OPEN}{_IS})?")
# Label words: "policy no", "policy number(s)", "policy ref / reference / id / identifier /
# certificate / contract", "policy reference number", "pol no." ... The word boundary after
# each keeps "policy note", "policy nobody" and "policy idea" intact. The value may be on the
# next line.
_WORD = (r"(?:(?:numbers?|nos?|num|nr|reference|ref|identifier|id|certificate|cert|contract)"
         r"\b\.?|#)")
_POLICY_LABELLED = re.compile(
    rf"(\bpol(?:icy)?\.?(?:{_WS}*|[-_]{_WS}*){_WORD}(?:(?:{_WS}+|[-_]{_WS}*){_WORD})*"
    rf"{_OPEN}{_IS}{_INDEX})" + _VALUE,
    re.IGNORECASE | re.ASCII)
# A bare "policy" followed by a number of 6+ digits/letters, after spaces or one of : - # =.
_POLICY_BARE = re.compile(
    rf"(\bpolicy(?:{_HS}*[:#=-]{_HS}*|{_HS}+){_INDEX})" + _VALUE, re.IGNORECASE | re.ASCII)
_DIGITS_ONLY = re.compile(rf"(?:[0-9]|{_HS}|-)+", re.ASCII)
_REMOVED = "[policy no. removed]"


def _is_sg_phone(value: str) -> bool:
    return bool(_DIGITS_ONLY.fullmatch(value)) and normalize_sg(value)[1]


def _keep_bare(m: re.Match) -> bool:
    # A Singapore phone number after the bare word is kept (Ruling: it cannot be told apart
    # from an 8-digit policy number). The labelled forms are always removed.
    return sum(ch.isalnum() for ch in m.group(2)) < 6 or _is_sg_phone(m.group(2))


# Characters that hide a number from a pattern without hiding it from a reader.
_INVISIBLE = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e")


def _fold_char(ch: str) -> str:
    """One character as the matcher sees it: full-width forms (U+FF01-FF5E) and other scripts'
    digits become plain ASCII."""
    o = ord(ch)
    if 0xFF01 <= o <= 0xFF5E:
        return chr(o - 0xFEE0)
    if o > 127 and ch.isdecimal():
        return str(unicodedata.decimal(ch))
    return ch


class _Work:
    """The text as matched (zero-width characters dropped, full-width written as ASCII), with for
    every matched character the exact piece of the ORIGINAL it stands for. Removing a number
    therefore leaves everything else exactly as typed."""

    def __init__(self, text: str):
        self.prefix = ""  # invisible characters before the first visible one
        chars: list[str] = []
        self.src: list[str] = []
        for ch in text:
            if ch in _INVISIBLE:
                if self.src:
                    self.src[-1] += ch
                else:
                    self.prefix += ch
                continue
            chars.append(_fold_char(ch))
            self.src.append(ch)
        self.text = "".join(chars)

    def sub(self, pattern: re.Pattern, decide) -> None:
        """decide(match) returns (start, replacement) to replace text[start:match.end()], or None."""
        out_text: list[str] = []
        out_src: list[str] = []
        pos = 0
        for m in pattern.finditer(self.text):
            hit = decide(m)
            if hit is None:
                continue
            start, repl = hit
            out_text.append(self.text[pos:start])
            out_src.extend(self.src[pos:start])
            out_text.append(repl)
            out_src.extend(repl)  # the inserted text is literal
            pos = m.end()
        out_text.append(self.text[pos:])
        out_src.extend(self.src[pos:])
        self.text, self.src = "".join(out_text), out_src

    def result(self) -> str:
        return self.prefix + "".join(self.src)


def has_nric(text: str) -> bool:
    return bool(_NRIC.search(_Work(text or "").text))


def redact(text: str) -> str:
    work = _Work(text or "")
    work.sub(_NRIC, lambda m: (m.start(), "[ID removed]"))
    work.sub(_POLICY_LABELLED, lambda m: (m.end(1), _REMOVED))
    work.sub(_POLICY_BARE, lambda m: None if _keep_bare(m) else (m.end(1), _REMOVED))
    return work.result()
