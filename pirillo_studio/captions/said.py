"""Words as said (docs/captions.md contract 6): a written number's spoken reading for the aligner, and the
transcriber's split tokens joined back into the one word the screen shows. Text is never reworded or recased."""
import re

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_ORDINAL = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
            "nine": "ninth", "twelve": "twelfth"}
CLITICS = {"'all", "'em", "'cause", "'til", "'bout", "'s", "'re", "'ll", "'ve", "'d", "'m", "'t", "'n"}
_SCALES = ((1000000000, "billion"), (1000000, "million"), (1000, "thousand"))


def words(n):
    """A whole number in words: 5795 -> five thousand seven hundred ninety five."""
    for size, name in _SCALES:
        if n >= size:
            rest = n % size
            return f"{words(n // size)} {name}" + (f" {words(rest)}" if rest else "")
    if n >= 100:
        return f"{_ONES[n // 100]} hundred" + (f" {words(n % 100)}" if n % 100 else "")
    if n >= 20:
        return _TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else "")
    return _ONES[n]


def _whole(digits):
    """A digit run as said: a year as a year, a leading zero digit by digit, else the number."""
    n = int(digits)
    if len(digits) == 4 and digits[0] != "0" and 1100 <= n <= 2099 and n % 1000 >= 10:
        low = n % 100
        return words(n // 100) + " " + ("hundred" if not low else f"oh {_ONES[low]}" if low < 10 else words(low))
    if digits[0] == "0" and len(digits) > 1:
        return " ".join(_ONES[int(c)] for c in digits)
    return words(n)


def say_number(token):
    """The words a written number is said as ("$5,795." -> five thousand seven hundred ninety five dollars), in
    the order they are said; None when the token holds no digit."""
    t = token.strip().strip(".,;:!?\"'()[]…").replace("’", "'")
    if not re.search(r"\d", t):
        return None
    m = re.fullmatch(r"(\d+)(st|nd|rd|th)", t.lower())
    if m:
        base = _whole(m.group(1)).split()
        last = _ORDINAL.get(base[-1]) or (base[-1][:-1] + "ieth" if base[-1].endswith("y") else base[-1] + "th")
        return " ".join(base[:-1] + [last])
    m = re.fullmatch(r"\$(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d\d))?", t)
    if m:
        whole = words(int(m.group(1).replace(",", "")))
        if m.group(2):
            cents = int(m.group(2))
            return f"{whole} " + (words(cents) if cents >= 10 else f"oh {_ONES[cents]}")
        return whole + (" dollar" if whole == "one" else " dollars")
    m = re.fullmatch(r"(\d{1,2}):(\d\d)", t)
    if m:
        minute = int(m.group(2))
        return words(int(m.group(1))) + ("" if not minute else f" oh {_ONES[minute]}" if minute < 10
                                         else f" {words(minute)}")
    m = re.fullmatch(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(%|s|k|K|m|M)?", t)
    if m:
        said = words(int(m.group(1).replace(",", ""))) if "," in m.group(1) else _whole(m.group(1))
        if m.group(2):
            said += " point " + " ".join(_ONES[int(c)] for c in m.group(2))
        suffix = m.group(3) or ""
        if suffix == "%":
            return said + " percent"
        if suffix == "s":  # the 1980s
            return said[:-1] + "ies" if said.endswith("y") else said + "s"
        return said + (" " + suffix.lower() if suffix else "")
    # anything else with digits (V2, 4x4, 1-2-3): each digit run read as a number, the rest as spelled
    return re.sub(r"\s+", " ", re.sub(r"\d+", lambda x: f" {_whole(x.group(0))} ", re.sub(r"[^\w$%]+", " ", t))).strip()


def said(text):
    """What the aligner should hear for a transcript word."""
    return say_number(text) or text


def _glue(previous, t):
    """Whether token t continues the word before it on screen."""
    return (len(t) > 1 and (t[0] in "-&" or (t[0] == "." and t[1].isalnum()) or (t[0] == "," and t[1].isdigit())
                            or (t[0] in "'’" and t[1].isdigit() and re.search(r"\d$", previous) is not None)
                            or t.lower().replace("’", "'").rstrip(".,!?;:") in CLITICS)
            or (t[:1] == "%" and t[1:2] != "%"))


def join(tokens):
    """Join split tokens ("cross" "-platform", "100" "%", "Y" "'all", ".com", "$2" ",000", "R" "&D", 5 '5")
    within one piece; a joined token keeps every ID and spans its parts' times."""
    out = []
    for token in tokens:
        if out and out[-1]["piece"] == token["piece"] and _glue(out[-1]["text"], token["text"]):
            last = out[-1]
            out[-1] = dict(last, text=last["text"] + token["text"], ids=last["ids"] + token["ids"], end=token["end"])
        else:
            out.append(dict(token))
    return out
