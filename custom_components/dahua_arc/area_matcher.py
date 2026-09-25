"""Smart, local-only matching of Dahua zone names to Home Assistant areas."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(frozen=True, slots=True)
class AreaCandidate:
    """Minimal area representation used by the matcher."""

    area_id: str
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AreaMatch:
    """One smart area match result."""

    area_id: str
    area_name: str
    score: int
    reason: str
    second_best_score: int = 0


# Hardware / alarm vocabulary should not influence room matching.
_GENERIC_TOKENS = {
    "alarm",
    "break",
    "contact",
    "detection",
    "detector",
    "dual",
    "flood",
    "front",
    "glass",
    "left",
    "magnetic",
    "mc",
    "motion",
    "pir",
    "radar",
    "right",
    "sensor",
    "shutter",
    "small",
    "smoke",
    "window",
    "water",
    "door",
}

# Canonical room/location vocabulary. This deliberately includes common Greek
# area names so English Dahua labels can still match a Greek HA installation.
_TOKEN_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "lr": ("living", "room"),
    "livingroom": ("living", "room"),
    "saloni": ("living", "room"),
    "σαλονι": ("living", "room"),
    "καθιστικο": ("living", "room"),
    "kitchen": ("kitchen",),
    "κουζινα": ("kitchen",),
    "office": ("office",),
    "γραφειο": ("office",),
    "bedroom": ("bedroom",),
    "υπνοδωματιο": ("bedroom",),
    "bathroom": ("bathroom",),
    "μπανιο": ("bathroom",),
    "λουτρο": ("bathroom",),
    "wc": ("wc",),
    "toilet": ("wc",),
    "τουαλετα": ("wc",),
    "garage": ("garage",),
    "γκαραζ": ("garage",),
    "attic": ("attic",),
    "σοφιτα": ("attic",),
    "stair": ("staircase",),
    "stairs": ("staircase",),
    "staircase": ("staircase",),
    "σκαλα": ("staircase",),
    "σκαλες": ("staircase",),
    "corridor": ("corridor",),
    "hall": ("corridor",),
    "hallway": ("corridor",),
    "διαδρομος": ("corridor",),
    "χολ": ("corridor",),
    "laundry": ("laundry",),
    "πλυσταριο": ("laundry",),
    "wardrobe": ("wardrobe",),
    "closet": ("wardrobe",),
    "guardaroba": ("wardrobe",),
    "ντουλαπα": ("wardrobe",),
    "apartment": ("apartment",),
    "διαμερισμα": ("apartment",),
    "garden": ("garden",),
    "backyard": ("garden",),
    "κηπος": ("garden",),
    "patio": ("patio",),
    "βεραντα": ("patio",),
    "terrace": ("terrace",),
    "first": ("first",),
    "1st": ("first",),
    "second": ("second",),
    "2nd": ("second",),
}

# Matches the integration's DEFAULT_AREA_MATCH_THRESHOLD; kept here so this
# module stays free of Home Assistant imports.
DEFAULT_THRESHOLD = 90


def _strip_diacritics(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _raw_tokens(value: str) -> list[str]:
    # Dahua labels sometimes contain glued CamelCase words, e.g.
    # WindowKitchen. Split those before case-folding.
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    value = _strip_diacritics(value).casefold()
    value = value.replace("'s", "s")
    # Greek letters are intentional: area names may be Greek.
    value = re.sub(r"[^\wα-ωάέήίόύώϊϋΐΰ]+", " ", value, flags=re.UNICODE)  # noqa: RUF001
    return [token for token in value.split() if token]


def canonical_tokens(value: str, *, drop_sensor_words: bool) -> tuple[str, ...]:
    """Return normalized semantic tokens for a label or area name."""

    result: list[str] = []
    for token in _raw_tokens(value):
        if drop_sensor_words and token in _GENERIC_TOKENS:
            continue
        expanded = _TOKEN_EXPANSIONS.get(token, (token,))
        for part in expanded:
            if drop_sensor_words and part in _GENERIC_TOKENS:
                continue
            result.append(part)
    return tuple(result)


def _score_variant(
    zone_tokens: tuple[str, ...], area_tokens: tuple[str, ...]
) -> tuple[int, str]:
    if not zone_tokens or not area_tokens:
        return 0, "no meaningful location tokens"

    area_set = set(area_tokens)
    # Possessive or plural labels ("Annas Window", "Anna's Window" after
    # apostrophe removal) should match an area named after the singular
    # ("Anna Office") without hard-coding any household's names.
    zone_tokens = tuple(
        token[:-1]
        if token not in area_set
        and len(token) > 3
        and token.endswith("s")
        and token[:-1] in area_set
        else token
        for token in zone_tokens
    )
    zone_set = set(zone_tokens)

    if zone_set == area_set:
        return 100, "same normalized location words"

    overlap = zone_set & area_set
    if not overlap:
        return 0, "no shared location words"

    # Area is completely described by the sensor label. Prefer multi-token
    # areas over a single incidental token (e.g. Living Room over Patio).
    if area_set <= zone_set:
        specificity = min(len(area_set), 4)
        return min(99, 92 + specificity * 2), "area name contained in zone label"

    # A short zone label can still strongly imply a richer area, e.g.
    # "Annas Window" -> "Anna Office". Ambiguity is handled later by the
    # second-best margin, so this remains safe when several Anna areas exist.
    if zone_set <= area_set:
        coverage = len(zone_set) / len(area_set)
        return round(86 + 10 * coverage), "zone location contained in area name"

    precision = len(overlap) / len(zone_set)
    recall = len(overlap) / len(area_set)
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0

    zone_text = " ".join(zone_tokens)
    area_text = " ".join(area_tokens)
    sequence = SequenceMatcher(None, zone_text, area_text).ratio()

    score = round(62 * f1 + 38 * sequence)
    return min(score, 95), "fuzzy semantic token match"


def match_zone_to_area(
    zone_name: str,
    areas: Iterable[AreaCandidate],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    min_margin: int = 6,
) -> AreaMatch | None:
    """Return a high-confidence area match or ``None`` when ambiguous.

    The matcher is intentionally conservative. It only returns a match when
    the best candidate meets the requested threshold and is sufficiently
    better than the second-best candidate. This prevents sensors containing
    only a person's name (for example) from being guessed into the wrong room.
    """

    zone_tokens = canonical_tokens(zone_name, drop_sensor_words=True)
    scored: list[tuple[int, str, AreaCandidate]] = []

    for area in areas:
        best_score = 0
        best_reason = ""
        for variant in (area.name, *area.aliases):
            area_tokens = canonical_tokens(variant, drop_sensor_words=False)
            score, reason = _score_variant(zone_tokens, area_tokens)
            if score > best_score:
                best_score = score
                best_reason = reason
        scored.append((best_score, best_reason, area))

    scored.sort(key=lambda item: (item[0], len(item[2].name)), reverse=True)
    if not scored:
        return None

    best_score, reason, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0

    if best_score < threshold:
        return None
    if second_score >= threshold and best_score - second_score < min_margin:
        return None

    return AreaMatch(
        area_id=best.area_id,
        area_name=best.name,
        score=best_score,
        reason=reason,
        second_best_score=second_score,
    )


def match_zone_with_hint(
    zone_name: str,
    area_hint: str | None,
    areas: Iterable[AreaCandidate],
    *,
    threshold: int = DEFAULT_THRESHOLD,
) -> AreaMatch | None:
    """Match a sensor using both Dahua subsystem metadata and its label.

    The two sources are independent evidence. If both produce confident but
    different HA areas, the integration deliberately refuses to guess. This
    catches stale/misconfigured Dahua subsystem assignments such as a sensor
    whose label clearly names a different room.
    """
    area_list = list(areas)
    name_match = match_zone_to_area(zone_name, area_list, threshold=threshold)
    hint_match = (
        match_zone_to_area(area_hint, area_list, threshold=threshold)
        if area_hint
        else None
    )

    if hint_match is not None and name_match is not None:
        if hint_match.area_id != name_match.area_id:
            return None
        # Same answer from two independent local signals. Preserve the stronger
        # score and make the reason explicit for diagnostics.
        stronger = hint_match if hint_match.score >= name_match.score else name_match
        return AreaMatch(
            area_id=stronger.area_id,
            area_name=stronger.area_name,
            score=max(hint_match.score, name_match.score),
            reason="Dahua subsystem and sensor label agree",
            second_best_score=max(
                hint_match.second_best_score, name_match.second_best_score
            ),
        )

    if hint_match is not None:
        return AreaMatch(
            area_id=hint_match.area_id,
            area_name=hint_match.area_name,
            score=hint_match.score,
            reason=f"Dahua subsystem: {hint_match.reason}",
            second_best_score=hint_match.second_best_score,
        )
    if name_match is not None:
        return AreaMatch(
            area_id=name_match.area_id,
            area_name=name_match.area_name,
            score=name_match.score,
            reason=f"sensor label: {name_match.reason}",
            second_best_score=name_match.second_best_score,
        )
    return None
