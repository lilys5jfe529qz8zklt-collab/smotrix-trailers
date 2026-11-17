from __future__ import annotations

import re
import time
from typing import Any

import aiohttp

from config import STORAGE_API_BASE, STORAGE_TOKEN

YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
TAG_RE = re.compile(r"[\[(（].*?[\])）]")
NOISE = (
    "официальный трейлер", "официальный тизер", "официальный", "финальный трейлер",
    "финальный", "новый трейлер", "новый", "трейлер", "тизер", "тизер-трейлер",
    "дубляж", "дублированный", "русский", "на русском", "в кино", "смотреть онлайн",
    "премьера", "эксклюзив", "международный", "official trailer", "official teaser",
    "official", "final trailer", "final", "new trailer", "trailer", "teaser",
    "dubbed", "russian", "in cinemas", "imax", "4k", "8k", "uhd", "hd", "1080p", "720p",
)
PUNCT_RE = re.compile(r"[^a-zа-яё0-9]+")


def norm(value: str) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return " ".join(PUNCT_RE.sub(" ", text).split())


def year_of(value: str) -> int:
    match = YEAR_RE.search(str(value or ""))
    return int(match.group(1)) if match else 0


def clean_title(raw: str) -> str:
    text = TAG_RE.sub(" ", str(raw or ""))
    text = text.replace("｜", "|").replace("—", " - ")
    lowered = text.casefold().replace("ё", "е")
    for noise in NOISE:
        lowered = lowered.replace(noise, " ")
    lowered = YEAR_RE.sub(" ", lowered)
    cleaned = " ".join(PUNCT_RE.sub(" ", lowered).split())
    return cleaned.strip()


SEQUEL_HINT_RE = re.compile(r"\b(часть|part|глава|chapter|том|volume)\b", re.I)


def variants(raw: str) -> list[str]:
    out: list[str] = []
    parts = [part.strip() for part in str(raw or "").split("|") if part.strip()]
    sources = parts or [str(raw or "")]
    for source in sources:
        base = clean_title(source)
        if base and base not in out:
            out.append(base)
        if SEQUEL_HINT_RE.search(source):
            stripped = SEQUEL_HINT_RE.sub(" ", source)
            stripped = re.sub(
                r"\b(вторая|второй|третья|третий|четвертая|четвертый|пятая|пятый|шестая|шестой)\b",
                " ",
                stripped,
                flags=re.I,
            )
            stripped = re.sub(r"\s+[ivxlcdm\d]{1,3}\b", " ", stripped, flags=re.I)
            short = clean_title(stripped)
            if short and short not in out:
                out.append(short)
        else:
            head = clean_title(source.split(":", 1)[0])
            if head and head not in out:
                out.append(head)
    return out[:6]


def _tokens(value: str) -> set[str]:
    return {token for token in norm(value).split() if len(token) > 1}


def _markers(value: str) -> set[str]:
    tokens = norm(value).split()
    out: set[str] = set()
    for index, token in enumerate(tokens):
        if token in {"часть", "part", "глава", "chapter", "том", "volume"} and index + 1 < len(tokens):
            out.add(f"{token}:{tokens[index + 1]}")
    return out


def _score(candidate: dict[str, Any], variant: str, year: int) -> int:
    names = [norm(candidate.get("title") or ""), norm(candidate.get("title_orig") or "")]
    wanted = norm(variant)
    if not wanted:
        return -1
    best = 0
    for name in names:
        if not name:
            continue
        if name == wanted:
            best = max(best, 100)
            continue
        if len(wanted) >= 4 and (wanted in name or name in wanted):
            best = max(best, 70)
            continue
        a, b = _tokens(wanted), _tokens(name)
        if not a or not b:
            continue
        overlap = len(a & b) / len(a | b)
        if overlap >= 0.7:
            best = max(best, 60)
        elif overlap >= 0.5:
            best = max(best, 45)
    markers = _markers(variant)
    if markers and not any(markers <= _markers(name) for name in names if name):
        best -= 70
    candidate_year = int(candidate.get("year") or 0)
    if year and candidate_year:
        if year == candidate_year:
            best += 25
        elif abs(year - candidate_year) == 1:
            best += 10
        else:
            best -= 30
    return best


class Matcher:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    async def _search(self, session: aiohttp.ClientSession, query: str) -> list[dict[str, Any]]:
        cached = self._cache.get(query)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        params = {"q": query, "available": "1"}
        if STORAGE_TOKEN:
            params["token"] = STORAGE_TOKEN
        url = STORAGE_API_BASE + "/api/index/search"
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as response:
                payload = await response.json(content_type=None)
            titles = payload.get("titles") or [] if isinstance(payload, dict) else []
        except Exception:
            titles = []
        self._cache[query] = (time.time(), titles)
        return titles

    async def find(self, session: aiohttp.ClientSession, youtube_title: str) -> dict[str, Any] | None:
        year = year_of(youtube_title)
        queries = variants(youtube_title)
        first = str(youtube_title or "").split("|", 1)[0]
        target = clean_title(first) or (queries[0] if queries else "")
        if not target:
            return None
        candidates: dict[int, tuple[int, dict[str, Any]]] = {}
        for query in queries:
            if len(query) < 3:
                continue
            for candidate in await self._search(session, query):
                title_id = int(candidate.get("id") or 0)
                if title_id <= 0 or int(candidate.get("stored") or 0) <= 0:
                    continue
                score = _score(candidate, target, year)
                previous = candidates.get(title_id)
                if previous is None or score > previous[0]:
                    candidates[title_id] = (score, candidate)
            if candidates and max(score for score, _ in candidates.values()) >= 125:
                break
        if not candidates:
            return None
        score, candidate = max(candidates.values(), key=lambda item: item[0])
        if score < 55:
            return None
        return candidate
