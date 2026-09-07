from collections.abc import Iterable
from datetime import datetime

from .match_policy import (
    DEFAULT_POLICY,
    MATCH_RULE_EDIT_DISTANCE,
    MATCH_RULE_EXACT,
    MATCH_RULE_OCR_CONFUSION,
    LevelRule,
    MatchPolicy,
    ResolvedPolicy,
)
from .models import MatchDecision, PlateObservation


MIN_EXACT_CONFIDENCE = 0.90
MIN_FUZZY_CONFIDENCE = 0.90
_CONFUSION_GROUPS = (frozenset(("0", "O")), frozenset(("1", "I", "L")),
                     frozenset(("2", "Z")), frozenset(("5", "S")),
                     frozenset(("8", "B")))
# A near miss is only ever reported for review. Anything further away than
# this is a different vehicle, not a misread.
MAX_NEAR_MISS_DISTANCE = 2


def normalise_plate(value: str) -> str:
    """Return the ASCII alphanumeric plate form used for exact comparisons."""
    return "".join(character for character in value.upper()
                   if character.isascii() and character.isalnum())


def decide_access(
    observations: Iterable[PlateObservation],
    authorised: Iterable[str],
    policy: MatchPolicy | None = None,
    *,
    now: datetime | None = None,
) -> MatchDecision:
    """Apply exact-first, fail-closed plate matching to OCR observations.

    ``policy`` selects the fuzziness level in force at ``now``. Omitting it
    keeps the controller's shipped behaviour: ``standard`` around the clock.
    """
    resolved = (policy or DEFAULT_POLICY).resolve(now)
    rule = resolved.rule
    authorised_plates = {normalise_plate(plate) for plate in authorised}
    authorised_plates.discard("")
    normalised_observations = [
        (normalise_plate(observation.plate), observation)
        for observation in observations
        if observation.plate
    ]

    for observed_plate, observation in normalised_observations:
        if (observed_plate and observed_plate in authorised_plates
                and observation.confidence >= MIN_EXACT_CONFIDENCE):
            return MatchDecision(
                allowed=True,
                reason="exact_match",
                authorised_plate=observed_plate,
                observed_plate=observed_plate,
                confidence=observation.confidence,
                match_rule=MATCH_RULE_EXACT,
                edit_distance=0,
                **_policy_fields(resolved),
            )

    if rule.allows_fuzzy:
        decision = _decide_fuzzy(
            normalised_observations, authorised_plates, rule, resolved
        )
        if decision is not None:
            return decision

    # Report the best-read plate on a denial so an unknown vehicle is
    # reviewable; it never widens the match.
    best = max(
        (item for item in normalised_observations if item[0]),
        key=lambda item: item[1].confidence,
        default=None,
    )
    if best is None:
        return MatchDecision(
            allowed=False, reason="no_match", **_policy_fields(resolved)
        )
    near_plate, near_distance = _nearest_authorised(best[0], authorised_plates)
    return MatchDecision(
        allowed=False, reason="no_match",
        observed_plate=best[0], confidence=best[1].confidence,
        near_miss_plate=near_plate, near_miss_distance=near_distance,
        **_policy_fields(resolved),
    )


def _decide_fuzzy(
    normalised_observations: list[tuple[str, PlateObservation]],
    authorised_plates: set[str],
    rule: LevelRule,
    resolved: ResolvedPolicy,
) -> MatchDecision | None:
    candidates: dict[str, dict[str, int]] = {}
    confidences: dict[str, float] = {}
    for observed_plate, observation in normalised_observations:
        if not observed_plate or observation.confidence < MIN_FUZZY_CONFIDENCE:
            continue
        if len(observed_plate) < rule.min_observed_length:
            continue
        matches = {}
        for plate in authorised_plates:
            distance = _match_distance(observed_plate, plate, rule)
            if distance is not None:
                matches[plate] = distance
        candidates.setdefault(observed_plate, {}).update(matches)
        confidences[observed_plate] = max(
            confidences.get(observed_plate, 0.0), observation.confidence
        )

    for observed_plate, matched_plates in candidates.items():
        frame_count = sum(
            1 for plate, observation in normalised_observations
            if plate == observed_plate and observation.confidence >= MIN_FUZZY_CONFIDENCE
        )
        if frame_count < rule.min_frames:
            continue
        if len(matched_plates) == 1:
            authorised_plate, distance = next(iter(matched_plates.items()))
            return MatchDecision(
                allowed=True,
                reason=_fuzzy_reason(rule),
                authorised_plate=authorised_plate,
                observed_plate=observed_plate,
                confidence=confidences[observed_plate],
                match_rule=(
                    MATCH_RULE_OCR_CONFUSION if rule.confusion_only
                    else MATCH_RULE_EDIT_DISTANCE
                ),
                edit_distance=distance,
                **_policy_fields(resolved),
            )
        if len(matched_plates) > 1:
            return MatchDecision(
                allowed=False,
                reason="ambiguous_fuzzy_match",
                observed_plate=observed_plate,
                confidence=confidences[observed_plate],
                **_policy_fields(resolved),
            )
    return None


def _fuzzy_reason(rule: LevelRule) -> str:
    # The shipped reason string is part of the event taxonomy the cloud reads,
    # so the standard level keeps saying exactly what it always said.
    if rule.confusion_only:
        return "two_frame_ocr_confusion"
    return "two_frame_edit_distance"


def _policy_fields(resolved: ResolvedPolicy) -> dict[str, str]:
    return {
        "policy_band": resolved.band,
        "policy_level": resolved.level,
        "policy_timezone": resolved.timezone_name,
        "policy_local_time": resolved.local_time,
    }


def _match_distance(
    observed_plate: str, authorised_plate: str, rule: LevelRule
) -> int | None:
    """Return the edit distance if this pair may match under ``rule``."""
    if not observed_plate or not authorised_plate:
        return None
    if rule.confusion_only:
        return 1 if _is_one_known_confusion(observed_plate, authorised_plate) else None
    if len(authorised_plate) < rule.min_observed_length:
        return None
    distance = _bounded_levenshtein(
        observed_plate, authorised_plate, rule.max_edit_distance
    )
    if distance is None or distance == 0:
        # Distance zero is an exact match and was already handled; reaching
        # here means the read failed the confidence gate, so do not revive it.
        return None
    return distance


def _nearest_authorised(
    observed_plate: str, authorised_plates: set[str]
) -> tuple[str | None, int | None]:
    """Return the closest authorised plate to a denied read, for review only."""
    best_plate: str | None = None
    best_distance: int | None = None
    for plate in authorised_plates:
        distance = _bounded_levenshtein(
            observed_plate, plate, MAX_NEAR_MISS_DISTANCE
        )
        if distance is None or distance == 0:
            continue
        if best_distance is None or distance < best_distance:
            best_plate, best_distance = plate, distance
        elif distance == best_distance and plate < (best_plate or ""):
            best_plate = plate
    return best_plate, best_distance


def _bounded_levenshtein(left: str, right: str, maximum: int) -> int | None:
    """Levenshtein distance, or ``None`` once it is known to exceed ``maximum``."""
    if abs(len(left) - len(right)) > maximum:
        return None
    previous = list(range(len(right) + 1))
    for row, left_character in enumerate(left, start=1):
        current = [row]
        for column, right_character in enumerate(right, start=1):
            current.append(min(
                previous[column] + 1,
                current[column - 1] + 1,
                previous[column - 1] + (left_character != right_character),
            ))
        if min(current) > maximum:
            return None
        previous = current
    distance = previous[-1]
    return distance if distance <= maximum else None


def _is_one_known_confusion(observed_plate: str, authorised_plate: str) -> bool:
    if len(observed_plate) != len(authorised_plate):
        return False
    differences = [
        (observed, expected)
        for observed, expected in zip(observed_plate, authorised_plate)
        if observed != expected
    ]
    return len(differences) == 1 and any(
        set(differences[0]).issubset(group) for group in _CONFUSION_GROUPS
    )
