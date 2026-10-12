"""Deterministic party screening for sanctions and trading restrictions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple
import re
import unicodedata

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.models import as_decimal


class RiskRating(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PROHIBITED = "prohibited"


class ScreeningDisposition(str, Enum):
    CLEAR = "clear"
    REVIEW = "review"
    BLOCK = "block"


def normalize_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^A-Za-z0-9 ]+", " ", ascii_value).upper().split())


def token_similarity(left: str, right: str) -> Decimal:
    left_tokens = set(normalize_name(left).split())
    right_tokens = set(normalize_name(right).split())
    if not left_tokens or not right_tokens:
        return Decimal("0")
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return Decimal(intersection) / Decimal(union)


@dataclass(frozen=True)
class PartyProfile:
    party_id: str
    legal_name: str
    country: str
    risk_rating: RiskRating
    aliases: Tuple[str, ...] = ()
    registration_id: Optional[str] = None
    politically_exposed: bool = False
    onboarding_date: Optional[date] = None

    def __post_init__(self) -> None:
        identifier = self.party_id.strip()
        name = self.legal_name.strip()
        country = self.country.strip().upper()
        if not identifier or not name:
            raise ValidationError("party id and legal name cannot be blank")
        if len(country) != 2 or not country.isalpha():
            raise ValidationError("party country must be a two-letter code")
        aliases = tuple(sorted({alias.strip() for alias in self.aliases if alias.strip()}))
        object.__setattr__(self, "party_id", identifier)
        object.__setattr__(self, "legal_name", name)
        object.__setattr__(self, "country", country)
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "risk_rating", self.risk_rating if isinstance(self.risk_rating, RiskRating) else RiskRating(self.risk_rating))


@dataclass(frozen=True)
class WatchlistEntry:
    entry_id: str
    name: str
    list_name: str
    countries: Tuple[str, ...] = ()
    aliases: Tuple[str, ...] = ()
    identifiers: Tuple[str, ...] = ()
    blocking: bool = True

    def names(self) -> Tuple[str, ...]:
        return (self.name,) + self.aliases


@dataclass(frozen=True)
class ScreeningMatch:
    entry_id: str
    list_name: str
    matched_name: str
    score: Decimal
    blocking: bool
    reason: str


@dataclass(frozen=True)
class ScreeningResult:
    party_id: str
    disposition: ScreeningDisposition
    matches: Tuple[ScreeningMatch, ...]
    country_restricted: bool
    identifier_match: bool

    @property
    def clear(self) -> bool:
        return self.disposition is ScreeningDisposition.CLEAR

    def as_dict(self) -> Mapping[str, object]:
        return {
            "partyId": self.party_id,
            "disposition": self.disposition.value,
            "countryRestricted": self.country_restricted,
            "identifierMatch": self.identifier_match,
            "matches": tuple({
                "entryId": item.entry_id,
                "list": item.list_name,
                "matchedName": item.matched_name,
                "score": str(item.score),
                "blocking": item.blocking,
                "reason": item.reason,
            } for item in self.matches),
        }


class ScreeningEngine:
    def __init__(self, *, review_threshold: object = Decimal("0.5"), block_threshold: object = Decimal("0.8")) -> None:
        review = as_decimal(review_threshold, "review threshold")
        block = as_decimal(block_threshold, "block threshold")
        if review < 0 or block > 1 or review > block:
            raise ValidationError("screening thresholds must be ordered within zero and one")
        self.review_threshold = review
        self.block_threshold = block
        self._watchlist: Dict[str, WatchlistEntry] = {}
        self._restricted_countries: set[str] = set()

    def restrict_country(self, country: str) -> None:
        normalized = country.strip().upper()
        if len(normalized) != 2:
            raise ValidationError("restricted country must be a two-letter code")
        self._restricted_countries.add(normalized)

    def add(self, entry: WatchlistEntry) -> None:
        existing = self._watchlist.get(entry.entry_id)
        if existing is not None and existing != entry:
            raise StorageConflictError("watchlist entry id conflicts with stored data")
        self._watchlist[entry.entry_id] = entry

    def screen(self, party: PartyProfile) -> ScreeningResult:
        matches = []
        identifier_match = False
        party_names = (party.legal_name,) + party.aliases
        for entry in self._watchlist.values():
            best_score = Decimal("0")
            matched_name = ""
            for party_name in party_names:
                for watch_name in entry.names():
                    score = token_similarity(party_name, watch_name)
                    if score > best_score:
                        best_score = score
                        matched_name = watch_name
            id_match = bool(party.registration_id and party.registration_id in entry.identifiers)
            identifier_match = identifier_match or id_match
            country_match = bool(entry.countries and party.country in entry.countries)
            if id_match:
                best_score = Decimal("1")
                reason = "registration identifier matched"
            elif best_score >= self.review_threshold:
                reason = "name similarity exceeded review threshold"
            elif country_match and best_score > 0:
                reason = "name and country partially matched"
            else:
                continue
            matches.append(ScreeningMatch(entry.entry_id, entry.list_name, matched_name, best_score, entry.blocking, reason))
        matches.sort(key=lambda item: (-item.score, item.entry_id))
        country_restricted = party.country in self._restricted_countries
        blocking = country_restricted or party.risk_rating is RiskRating.PROHIBITED or any(
            item.blocking and item.score >= self.block_threshold for item in matches
        )
        disposition = ScreeningDisposition.BLOCK if blocking else (
            ScreeningDisposition.REVIEW if matches or party.politically_exposed or party.risk_rating is RiskRating.HIGH else ScreeningDisposition.CLEAR
        )
        return ScreeningResult(party.party_id, disposition, tuple(matches), country_restricted, identifier_match)
