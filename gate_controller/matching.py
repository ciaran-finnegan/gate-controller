from collections.abc import Iterable

from .models import MatchDecision, PlateObservation


MIN_EXACT_CONFIDENCE = 0.90
MIN_FUZZY_CONFIDENCE = 0.90
_CONFUSION_GROUPS = (frozenset(("0", "O")), frozenset(("1", "I", "L")),
                     frozenset(("2", "Z")), frozenset(("5", "S")),
                     frozenset(("8", "B")))


def normalise_plate(value: str) -> str:
    """Return the ASCII alphanumeric plate form used for exact comparisons."""
    return "".join(character for character in value.upper()
                   if character.isascii() and character.isalnum())


def decide_access(
    observations: Iterable[PlateObservation], authorised: Iterable[str]
) -> MatchDecision:
    """Apply exact-first, fail-closed plate matching to OCR observations."""
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
            )

    candidates: dict[str, set[str]] = {}
    confidences: dict[str, float] = {}
    for observed_plate, observation in normalised_observations:
        if not observed_plate or observation.confidence < MIN_FUZZY_CONFIDENCE:
            continue
        candidates.setdefault(observed_plate, set()).update(
            plate for plate in authorised_plates
            if _is_one_known_confusion(observed_plate, plate)
        )
        confidences[observed_plate] = max(
            confidences.get(observed_plate, 0.0), observation.confidence
        )

    for observed_plate, matched_plates in candidates.items():
        frame_count = sum(
            1 for plate, observation in normalised_observations
            if plate == observed_plate and observation.confidence >= MIN_FUZZY_CONFIDENCE
        )
        if frame_count < 2:
            continue
        if len(matched_plates) == 1:
            return MatchDecision(
                allowed=True,
                reason="two_frame_ocr_confusion",
                authorised_plate=next(iter(matched_plates)),
                observed_plate=observed_plate,
                confidence=confidences[observed_plate],
            )
        if len(matched_plates) > 1:
            return MatchDecision(
                allowed=False,
                reason="ambiguous_fuzzy_match",
                observed_plate=observed_plate,
                confidence=confidences[observed_plate],
            )

    # Report the best-read plate on a denial so an unknown vehicle is
    # reviewable; it never widens the match.
    best = max(
        normalised_observations,
        key=lambda item: item[1].confidence,
        default=None,
    )
    if best is None or not best[0]:
        return MatchDecision(allowed=False, reason="no_match")
    return MatchDecision(
        allowed=False, reason="no_match",
        observed_plate=best[0], confidence=best[1].confidence,
    )


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
