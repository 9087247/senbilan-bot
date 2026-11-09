"""Qo'lda yozilgan "Kimdan" ismini 1C kontragentlari bilan solishtirish."""
import re
from dataclasses import dataclass

from rapidfuzz import fuzz

AUTO_SCORE = 88
AUTO_GAP = 8
CANDIDATE_SCORE = 50

_CYR_TO_LAT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d", "е": "e", "ё": "yo",
        "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "қ": "q", "л": "l", "м": "m",
        "н": "n", "о": "o", "ў": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "x", "ҳ": "h", "ц": "s", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "",
        "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
)
_LEGAL_FORMS = {"ooo", "chp", "mchj", "qk", "xk", "ok", "oak"}


def normalize_name(name: str) -> str:
    s = re.sub(r"\([^)]*\)", " ", name.lower())
    s = s.translate(_CYR_TO_LAT)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in _LEGAL_FORMS)


@dataclass
class Counterparty:
    id: int
    name: str
    inn: str
    contract: str | None
    comment: str | None


def rank(raw: str, counterparties: list[Counterparty]) -> list[tuple[Counterparty, float]]:
    query = normalize_name(raw)
    if not query:
        return []
    scored = [
        (cp, max(fuzz.token_sort_ratio(query, n), fuzz.token_set_ratio(query, n)))
        for cp in counterparties
        if (n := normalize_name(cp.name))
    ]
    return sorted(scored, key=lambda pair: pair[1], reverse=True)


def auto_match(ranked: list[tuple[Counterparty, float]]) -> Counterparty | None:
    """Faqat bitta nomzod aniq ajralib tursagina avtomatik tanlaydi."""
    if not ranked or ranked[0][1] < AUTO_SCORE:
        return None
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < AUTO_GAP:
        return None
    return ranked[0][0]


def candidates(ranked: list[tuple[Counterparty, float]], limit: int = 4) -> list[Counterparty]:
    return [cp for cp, score in ranked[:limit] if score >= CANDIDATE_SCORE]
