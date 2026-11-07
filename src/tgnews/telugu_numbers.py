"""Write numbers, prices, percentages, dates and times the way a Telugu news reader says them.

Neural voices read digits badly in Telugu ("25,000" came out wrong), so the text that is SPOKEN (and shown
as the reel's captions, so the two stay in step) spells them out: 25,000 -> ఇరవై ఐదు వేలు. The caption text
and the poster text keep their digits. A number this module does not understand is left as it is.
"""
from __future__ import annotations

import re

_ONES = ("సున్నా", "ఒకటి", "రెండు", "మూడు", "నాలుగు", "ఐదు", "ఆరు", "ఏడు", "ఎనిమిది", "తొమ్మిది", "పది",
         "పదకొండు", "పన్నెండు", "పదమూడు", "పద్నాలుగు", "పదిహేను", "పదహారు", "పదిహేడు", "పద్దెనిమిది",
         "పంతొమ్మిది")
_TENS = {2: "ఇరవై", 3: "ముప్పై", 4: "నలభై", 5: "యాభై", 6: "అరవై", 7: "డెబ్భై", 8: "ఎనభై", 9: "తొంభై"}
_MONTHS = ("జనవరి", "ఫిబ్రవరి", "మార్చి", "ఏప్రిల్", "మే", "జూన్", "జూలై", "ఆగస్టు", "సెప్టెంబర్", "అక్టోబర్",
           "నవంబర్", "డిసెంబర్")
_TELUGU_DIGITS = str.maketrans("౦౧౨౩౪౫౬౭౮౯", "0123456789")
_LIMIT = 10 ** 9


def words(n: int) -> str:
    """A whole number in Telugu words (0 to 999,999,999); larger numbers come back as digits."""
    if n < 0 or n >= _LIMIT:
        return str(n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        return _TENS[tens] + (f" {_ONES[unit]}" if unit else "")
    if n < 1000:
        h, rest = divmod(n, 100)
        if h == 1:
            return "వంద" if not rest else f"నూట {words(rest)}"
        return f"{_ONES[h]} వందలు" if not rest else f"{_ONES[h]} వందల {words(rest)}"
    for size, one, plural, plural_of in ((10 ** 7, "కోటి", "కోట్ల", "కోట్లు"), (10 ** 5, "లక్ష", "లక్షల", "లక్షలు"),
                                         (1000, "వెయ్యి", "వేల", "వేలు")):
        if n >= size:
            head, rest = divmod(n, size)
            if head == 1:
                return one if not rest else f"{one} {words(rest)}"
            return f"{words(head)} {plural_of if not rest else plural}" + (f" {words(rest)}" if rest else "")
    return str(n)  # unreachable


def _daypart(hour24: int) -> str:
    return "ఉదయం" if 4 <= hour24 < 12 else "మధ్యాహ్నం" if hour24 < 16 else "సాయంత్రం" if hour24 < 19 else "రాత్రి"


_DATE = re.compile(r"(?<![\w.])(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?![\w.])")
_TIME = re.compile(r"(?<![\w.:])(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?(?![\w])")
_PERCENT = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*%")
_RUPEE = re.compile(r"(?:₹|\bRs\.?|\bINR)\s*(\d[\d,]*(?:\.\d+)?)", re.I)
_DECIMAL = re.compile(r"(?<![\w.])(\d[\d,]*)\.(\d+)(?![\w.])")
_INTEGER = re.compile(r"(?<![\w.])(\d[\d,]*)(?![\w])")


def _int(text: str) -> int:
    return int(text.replace(",", ""))


def _decimal(whole: str, frac: str) -> str:
    return f"{words(_int(whole))} పాయింట్ " + " ".join(_ONES[int(d)] for d in frac)


def _num(text: str) -> str:
    return _decimal(*text.split(".", 1)) if "." in text else words(_int(text))


def _date(m: re.Match) -> str:
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return m.group(0)
    return f"{words(day)} {_MONTHS[month - 1]} {words(year)}"


def _time(m: re.Match) -> str:
    hour, minute, pm = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower() == "p"
    if not (1 <= hour <= 12 and 0 <= minute < 60):
        return m.group(0)
    hour24 = hour % 12 + (12 if pm else 0)
    part = _daypart(hour24)
    return f"{part} {words(hour)} గంటలకు" if not minute else f"{part} {words(hour)} గంటల {words(minute)} నిమిషాలకు"


def _before_noun(number_words: str) -> str:
    """"ఇరవై ఐదు వేలు" -> "ఇరవై ఐదు వేల": the plural drops its ending when a noun follows ("వేల రూపాయలు")."""
    return number_words[:-1] if number_words.endswith(("వేలు", "వందలు", "లక్షలు", "కోట్లు")) else number_words


_NOUN_NEXT = re.compile(r"\s+[^\W\d_]")


def speakable(text: str) -> str:
    """`text` with its numbers, prices, percentages, dates and times written out in Telugu words."""
    text = text.translate(_TELUGU_DIGITS)
    text = _DATE.sub(_date, text)
    text = _TIME.sub(_time, text)
    text = _PERCENT.sub(lambda m: f"{_before_noun(_num(m.group(1)))} శాతం", text)
    text = _RUPEE.sub(lambda m: f"{_before_noun(_num(m.group(1)))} రూపాయలు", text)
    text = _DECIMAL.sub(lambda m: _decimal(m.group(1), m.group(2)), text)

    def whole(m: re.Match) -> str:
        spoken = words(_int(m.group(1)))
        return _before_noun(spoken) if _NOUN_NEXT.match(m.string, m.end()) else spoken

    return _INTEGER.sub(whole, text)
