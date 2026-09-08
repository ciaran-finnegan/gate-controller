import logging
from collections.abc import Iterable
from datetime import datetime
from math import isfinite

from .match_policy import (
    DEFAULT_POLICY,
    LEGACY_MIN_CONFIDENCE,
    MATCH_RULE_EDIT_DISTANCE,
    MATCH_RULE_EXACT,
    MATCH_RULE_OCR_CONFUSION,
    LevelRule,
    MatchPolicy,
    ResolvedPolicy,
)
from .models import MatchDecision, PlateObservation


LOGGER = logging.getLogger(__name__)

#: The bars are per level now (``LevelRule.min_exact_confidence`` and
#: ``min_fuzzy_confidence``, set from ``GATE_MATCH_*`` -- see
#: ``docs/plate-matching.md``). These two names are kept because they are what
#: the controller required before the schedule existed, and because they are
#: still what a rejected environment value falls back to.
MIN_EXACT_CONFIDENCE = LEGACY_MIN_CONFIDENCE
MIN_FUZZY_CONFIDENCE = LEGACY_MIN_CONFIDENCE

SOURCE_LOCAL = "local"
SOURCE_CLOUD = "cloud"
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
    corroborations: Iterable[PlateObservation] = (),
) -> MatchDecision:
    """Apply exact-first, fail-closed plate matching to OCR observations.

    ``policy`` selects the fuzziness level in force at ``now``. Omitting it
    keeps the controller's shipped behaviour: ``standard`` around the clock.

    ``corroborations`` are reads of the *same event* by the other reader --
    in practice the on-device recogniser's reads of the frames the cloud
    answered for. They never take part in the exact or fuzzy rules above, so
    those two behave exactly as they always have; they exist only so the
    agreement rule below can see that two independent readers produced the
    same string. Passing none disables the agreement rule entirely.
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
                and observation.confidence >= rule.min_exact_confidence):
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

    decision = _decide_agreement(
        normalised_observations, corroborations, authorised_plates, rule,
        resolved,
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
        if not observed_plate or observation.confidence < rule.min_fuzzy_confidence:
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
            if plate == observed_plate and observation.confidence >= rule.min_fuzzy_confidence
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


def _decide_agreement(
    normalised_observations: list[tuple[str, PlateObservation]],
    corroborations: Iterable[PlateObservation],
    authorised_plates: set[str],
    rule: LevelRule,
    resolved: ResolvedPolicy,
) -> MatchDecision | None:
    """Two independent readers, the same string, a lower bar on each.

    The exact and fuzzy rules above ask one reader for a confident read. This
    one asks two readers -- the cloud service and the on-device recogniser --
    for the *same* normalised plate, and in exchange lowers what each has to
    carry to ``agreement_min_cloud_confidence`` and
    ``agreement_min_local_confidence`` for the band in force.

    It is deliberately narrow, and every narrowing is a fail-closed one:

    * both readers must be present. One reader agreeing with itself across two
      frames is the two-frame fuzzy rule, not this one, and a controller with
      no on-device reader can never reach this branch at all.
    * the agreed plate must still authorise under *this band's* rule -- exact
      membership, or the band's own one-confusion rule against a single
      authorised candidate *on the number of frames that band requires*.
      Agreement lowers what a read has to carry; it never buys an extra edit,
      never buys a frame, and under ``strict`` it never buys anything but an
      exact match.
    * a non-finite confidence (``nan``, ``inf``) is not a confidence. It is
      dropped before any comparison, so it cannot walk through a ``>=``.

    Callers key ``corroborations`` by trace id, so reads of one event can
    never corroborate another.
    """
    readers_by_plate: dict[str, dict[str, list]] = {}

    def remember(plate: str | None, observation) -> None:
        plate = normalise_plate(plate or "")
        if not plate:
            return
        try:
            confidence = float(getattr(observation, "confidence", 0.0))
        except (TypeError, ValueError):
            return
        if not isfinite(confidence):
            return
        source = getattr(observation, "source", SOURCE_CLOUD) or SOURCE_CLOUD
        minimum = (
            rule.agreement_min_local_confidence if source == SOURCE_LOCAL
            else rule.agreement_min_cloud_confidence
        )
        if confidence < minimum:
            return
        readers = readers_by_plate.setdefault(plate, {})
        seen = readers.setdefault(source, [confidence, 0])
        seen[0] = max(seen[0], confidence)
        # Each reader reads each frame at most once, so its own count is a
        # lower bound on the frames that carried this plate.
        seen[1] += 1

    for observed_plate, observation in normalised_observations:
        remember(observed_plate, observation)
    for observation in corroborations or ():
        remember(getattr(observation, "plate", None), observation)

    for plate in sorted(readers_by_plate):
        readers = readers_by_plate[plate]
        local = readers.get(SOURCE_LOCAL)
        cloud = readers.get(SOURCE_CLOUD)
        if local is None or cloud is None:
            continue
        match_rule, distance, authorised_plate = _agreement_match(
            plate, authorised_plates, rule, max(local[1], cloud[1]),
        )
        if match_rule is None:
            continue
        local, cloud = local[0], cloud[0]
        _log_agreement(plate, local, cloud, match_rule, rule, resolved)
        return MatchDecision(
            allowed=True,
            reason=(
                "exact_match" if match_rule == MATCH_RULE_EXACT
                else _fuzzy_reason(rule)
            ),
            authorised_plate=authorised_plate,
            observed_plate=plate,
            confidence=max(local, cloud),
            match_rule=match_rule,
            edit_distance=distance,
            **_policy_fields(resolved),
        )
    return None


def _agreement_match(
    plate: str, authorised_plates: set[str], rule: LevelRule, frames: int,
) -> tuple[str | None, int | None, str | None]:
    """How an agreed plate authorises under ``rule``, if it does at all.

    ``frames`` is how many frames of this event carried the plate, so the
    band's own two-frame requirement still stands for a non-exact match. An
    exact match has never needed a second frame and does not gain one here.
    """
    if plate in authorised_plates:
        return MATCH_RULE_EXACT, 0, plate
    if not rule.allows_fuzzy or len(plate) < rule.min_observed_length:
        return None, None, None
    if frames < rule.min_frames:
        return None, None, None
    matches = {
        candidate: distance
        for candidate, distance in (
            (candidate, _match_distance(plate, candidate, rule))
            for candidate in authorised_plates
        )
        if distance is not None
    }
    if len(matches) != 1:
        # Nothing close, or close to more than one authorised plate. Both are
        # denials; agreement never picks a favourite.
        return None, None, None
    candidate, distance = next(iter(matches.items()))
    match_rule = (
        MATCH_RULE_OCR_CONFUSION if rule.confusion_only
        else MATCH_RULE_EDIT_DISTANCE
    )
    return match_rule, distance, candidate


def _log_agreement(plate, local, cloud, match_rule, rule, resolved) -> None:
    """Journal-only. The wire reason stays the one the app already knows."""
    try:
        LOGGER.info(
            "gate_match stage=agreement_grant plate=%s local_score=%.3f "
            "cloud_score=%.3f match_rule=%s level=%s band=%s "
            "min_local=%.2f min_cloud=%.2f",
            plate, local, cloud, match_rule, resolved.level, resolved.band,
            rule.agreement_min_local_confidence,
            rule.agreement_min_cloud_confidence,
        )
    except Exception:
        return


def _fuzzy_reason(rule: LevelRule) -> str:
    # The shipped reason string is part of the event taxonomy the cloud reads,
    # so the standard level keeps saying exactly what it always said. No level
    # this release ships reaches the second branch.
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
    """Return the edit distance if this pair may match under ``rule``.

    Every level this release ships is ``confusion_only``, so the free-edit
    branch below is unreachable from settings. It is kept because a future
    level may want it, not because anything selects it today.
    """
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
