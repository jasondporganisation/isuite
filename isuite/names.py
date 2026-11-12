"""Pick the name to greet someone by. Singapore names come in several orders, so this
only picks a given name when the order is clear, and otherwise keeps the name whole."""
import re

_TITLES = {"mr", "mrs", "ms", "miss", "mdm", "madam", "dr", "prof"}
# Common Singapore family names written first, as in "Tan Wei Ming".
_FAMILY = {
    "ang", "au", "aw", "chan", "chang", "chee", "chen", "cheng", "cheong", "chew",
    "chia", "chiam", "chin", "chong", "choo", "chow", "chua", "chue", "foo", "fong",
    "gan", "goh", "han", "heng", "ho", "hoe", "huang", "kang", "khoo", "koh", "kok",
    "kong", "kua", "kwek", "kwok", "lai", "lam", "lau", "lee", "leong", "leow", "ler",
    "li", "liang",
    "liew", "lim", "lin", "liu", "loh", "loo", "low", "lu", "lui", "lum", "lye", "mok",
    "neo", "ng", "ngiam", "oh", "ong", "ooi", "pang", "peh", "pek", "phua", "poh", "png",
    "quek", "seah", "seow", "sia", "sim", "sng", "soh", "soon", "tan", "tang", "tay",
    "teh", "ting", "tng",
    "teo", "teoh", "tham", "thio", "toh", "tong", "wang", "wee", "wong", "woo",
    "wu", "yang", "yap", "yeo", "yeoh", "yong", "yu", "zhang", "zhao", "zhou", "zhu",
}
# Two-word family names. A name starting with one of these is kept whole.
_DOUBLE = {("au", "yeong"), ("au", "yong"), ("ou", "yang"), ("sim", "ah"), ("si", "tu"),
           ("see", "toh"), ("szeto", "")}
# Common first words that are not used alone; the next word goes with them.
_PREFIXES = {"muhammad", "muhamad", "mohamed", "mohammad", "mohammed", "mohd", "muhd",
             "nur", "nurul", "siti", "abdul", "sri"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md"}
_PARENS = re.compile(r"[(\[{（][^)\]}）]*[)\]}）]?")
_WORD = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*")
MAX_LEN = 30


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def first_name(name: str | None) -> str:
    """The name to use after "Hi". This greeting goes to a real person, so a given name
    is picked only when the order of the name is clear. Otherwise the whole name is
    kept ("Hi Tan Mei" is fine in Singapore), and "there" is used when nothing fits.

    "Lim Ah Kow, Daniel"  -> "Daniel"    (given name after the comma)
    "Tan Wei Ming"        -> "Wei Ming"  (family name, then a two-word given name)
    "Tan Wei Ming Daniel" -> "Daniel"    (family, given, English name)
    "Daniel Tan"          -> "Daniel"    (first word is not a family name)
    "Tan Mei"             -> "Tan Mei"   (two words, order unclear: kept whole)
    """
    text = " ".join(_PARENS.sub(" ", name or "").split())
    before, comma, after = text.rpartition(",")
    tail = _clean(_words(after)) if comma else []
    if 1 <= len(tail) <= 2:  # "Family Given, English"; a longer tail is not a name
        return _clip(tail[0])
    words = _clean(_words(before if comma else text))
    if not words:
        return "there"
    whole = _clip(" ".join(words))
    if len(words) == 1:
        return whole
    low = [w.lower() for w in words]
    if (low[0], low[1]) in _DOUBLE or low[0] == "ah":
        return whole
    if low[0] in _FAMILY:
        if len(words) == 3 and low[1] not in _FAMILY:
            return _clip(" ".join(words[1:]))
        if len(words) == 4 and low[3] not in _FAMILY:
            return _clip(words[3])
        return whole
    if low[0] in _PREFIXES and low[1] not in ("bin", "binte", "binti", "bte", "bt"):
        return _clip(" ".join(words[:2]))  # "Nur Aisyah binte Ahmad" -> "Nur Aisyah"
    return _clip(words[0])


def _clean(words: list[str]) -> list[str]:
    """Drop titles at the front and suffixes such as Jr at the end."""
    while words and words[0].lower() in _TITLES:
        words = words[1:]
    while words and words[-1].lower() in _SUFFIXES:
        words = words[:-1]
    return words


def _clip(value: str) -> str:
    """A greeting name is short. One that is too long is not cut mid-word; it is dropped."""
    return value if value and len(value) <= MAX_LEN else "there"
