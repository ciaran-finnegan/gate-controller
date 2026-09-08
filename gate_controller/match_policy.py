"""Time-of-day plate matching policy.

The controller matches an OCR read against the authorised snapshot with a
*fuzziness level*. A level is a named rule, not a raw number, so the owner
picks a risk posture rather than an edit distance:

``strict``
    Exact normalised plate only. Nothing else opens the gate.
``standard``
    Today's shipped behaviour: equal length, exactly one known OCR-confusion
    substitution (``0/O``, ``1/I/L``, ``2/Z``, ``5/S``, ``8/B``), two
    high-confidence frames, and a unique authorised candidate.

Those two are the only levels this release ships. A third, ``relaxed`` — two
edits of any kind against a read of at least six characters — was withdrawn
before release: see :data:`WITHDRAWN_LEVELS` and ``docs/plate-matching.md`` for
the measurement that killed it. A band naming it is treated as naming a level
this controller has never heard of, and becomes ``strict``.

A :class:`MatchPolicy` maps local-time bands onto those levels, so a site can
run ``standard`` by day and ``strict`` overnight. Bands are expressed in the
site's own timezone (Europe/Dublin by default) and must tile the 24-hour clock
exactly once: no gaps, no overlaps.

Everything here fails closed. An unreadable payload, an unknown level, a
schedule with a hole in it, or a timezone the platform cannot load all resolve
to the strictest available behaviour rather than a wider match.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import isfinite
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOGGER = logging.getLogger(__name__)

#: Version of the ``plate_matching`` settings document this controller reads.
#: The cloud may send a newer document; anything it does not recognise is
#: rejected rather than guessed at.
SCHEMA_VERSION = 1

DEFAULT_TIMEZONE = "Europe/Dublin"
MINUTES_PER_DAY = 24 * 60
MAX_BANDS = 12

LEVEL_STRICT = "strict"
LEVEL_STANDARD = "standard"

#: Levels that once existed and are no longer selectable. They are named here
#: only so a schedule that mentions one is logged as withdrawn rather than as a
#: typo; :func:`level_rule` and :func:`_parse_band` treat them as unknown, which
#: means ``strict``.
WITHDRAWN_LEVELS = frozenset({"relaxed"})

MATCH_RULE_EXACT = "exact"
MATCH_RULE_OCR_CONFUSION = "ocr_confusion"
MATCH_RULE_EDIT_DISTANCE = "edit_distance"


#: What the controller required of every read before the thresholds moved onto
#: the level. It is also where a *bad* environment value lands: an unreadable
#: knob must not silently choose the laxer new default.
LEGACY_MIN_CONFIDENCE = 0.90

#: The lowest bar an operator may configure. Below this a "bar" is not a bar:
#: ``GATE_MATCH_MIN_CONFIDENCE_STANDARD=0`` admits a 0.0-confidence exact read,
#: and ``1e-9`` is the same thing with a decimal point in it. Both readers
#: score far above 0.10 even on garbage -- the measured separation on this site
#: is 0.998 on correct reads against 0.772 on wrong ones, and the lowest bar
#: anything here ships with is the 0.50 local agreement bar -- so 0.10 is
#: already five times laxer than the laxest shipped value while still being a
#: number the reader can fail. A value below it is a typo or a disabled bar,
#: not a posture, and is refused like any other unusable value.
MIN_CONFIGURABLE_CONFIDENCE = 0.10

#: The confidence bars, per level. ``field`` is the :class:`LevelRule`
#: attribute, ``prefix`` the environment key stem (``<prefix>_STANDARD`` /
#: ``<prefix>_STRICT``), and the mapping the shipped default for each level.
#:
#: The daytime (``standard``) exact bar sits well below the overnight one on
#: purpose. An exact match is a character-for-character equality with an
#: authorised plate: to open the gate wrongly the reader has to hallucinate a
#: specific eight-character registration, which no low-confidence read does by
#: accident. A *fuzzy* match is a read that is deliberately not the authorised
#: plate, so its bar stays higher. Overnight, ``strict`` keeps every bar at
#: the historical 0.90 and is unchanged by this release.
CONFIDENCE_FIELDS = (
    ("min_exact_confidence", "GATE_MATCH_MIN_CONFIDENCE", {"strict": 0.90, "standard": 0.75}),
    ("min_fuzzy_confidence", "GATE_MATCH_MIN_FUZZY_CONFIDENCE", {"strict": 0.90, "standard": 0.85}),
    ("agreement_min_local_confidence", "GATE_MATCH_AGREEMENT_MIN_LOCAL_CONFIDENCE",
     {"strict": 0.90, "standard": 0.50}),
    ("agreement_min_cloud_confidence", "GATE_MATCH_AGREEMENT_MIN_CLOUD_CONFIDENCE",
     {"strict": 0.90, "standard": 0.70}),
)


class MatchPolicyError(ValueError):
    """A settings document could not be read as a plate matching policy."""


@dataclass(frozen=True)
class LevelRule:
    """How far a read may stray from an authorised plate at one level."""

    name: str
    #: Maximum edit distance a non-exact match may carry. ``0`` disables
    #: fuzzy matching entirely.
    max_edit_distance: int
    #: Whether a difference must be one of the known OCR confusion pairs at
    #: equal length (``True``) or may be any edit (``False``). Every level this
    #: release ships sets this ``True``; the free-edit path in
    #: :mod:`gate_controller.matching` is kept for a future level and is
    #: unreachable from settings.
    confusion_only: bool
    #: High-confidence frames that must agree before a non-exact match opens.
    min_frames: int
    #: Shortest normalised read eligible for a non-exact match. A two-edit
    #: budget against a three-character read is noise, not a plate.
    min_observed_length: int
    #: Confidence a single read must carry to open the gate on an exact,
    #: character-for-character match with an authorised plate.
    min_exact_confidence: float = LEGACY_MIN_CONFIDENCE
    #: Confidence each of the two frames must carry for a non-exact match.
    min_fuzzy_confidence: float = LEGACY_MIN_CONFIDENCE
    #: When the on-device reader and the cloud reader independently produced
    #: the *same* normalised plate for the same event, each only has to clear
    #: its own bar below. See :func:`gate_controller.matching.decide_access`.
    agreement_min_local_confidence: float = LEGACY_MIN_CONFIDENCE
    agreement_min_cloud_confidence: float = LEGACY_MIN_CONFIDENCE

    @property
    def allows_fuzzy(self) -> bool:
        return self.max_edit_distance > 0


LEVELS: dict[str, LevelRule] = {
    LEVEL_STRICT: LevelRule(
        name=LEVEL_STRICT,
        max_edit_distance=0,
        confusion_only=True,
        min_frames=2,
        min_observed_length=0,
    ),
    LEVEL_STANDARD: LevelRule(
        name=LEVEL_STANDARD,
        max_edit_distance=1,
        confusion_only=True,
        min_frames=2,
        # Equal length is already required by the confusion rule, so no extra
        # floor is needed here; keeping it at zero makes ``standard`` byte-for
        # -byte identical to the behaviour the controller shipped before the
        # schedule existed.
        min_observed_length=0,
    ),
}

#: Ordered strictest-first, so an unusable schedule can fall back to the
#: safest level without a lookup table of its own.
LEVEL_ORDER = (LEVEL_STRICT, LEVEL_STANDARD)


def confidence_environment_key(prefix: str, level: str) -> str:
    return f"{prefix}_{level.upper()}"


def _confidence_value(raw: object, key: str, default: float,
                      warned: set[str]) -> float:
    """Read one confidence knob, failing closed to the historical 0.90.

    A value that is not a number, is not finite (``nan`` and ``inf`` both),
    or falls outside :data:`MIN_CONFIGURABLE_CONFIDENCE`-1 is refused --
    ``0`` and ``1e-9`` included, because a bar no read can fail is not a bar.
    It does **not** fall back to this release's laxer default: a knob the
    operator meant to set and mistyped must leave the gate no wider than it
    was before the knob existed.
    """
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        value = float(text)
    except (TypeError, ValueError):
        value = None
    if (value is None or not isfinite(value)
            or not MIN_CONFIGURABLE_CONFIDENCE <= value <= 1.0):
        if key not in warned:
            warned.add(key)
            LOGGER.warning(
                "match_confidence key=%s status=rejected using=%.2f", key,
                LEGACY_MIN_CONFIDENCE,
            )
        return LEGACY_MIN_CONFIDENCE
    return value


def load_confidence(environment=None) -> dict[str, dict[str, float]]:
    """The per-level confidence bars in force, from ``GATE_MATCH_*``."""
    environment = os.environ if environment is None else environment
    warned: set[str] = set()
    thresholds: dict[str, dict[str, float]] = {}
    for level in LEVEL_ORDER:
        fields = {}
        for field, prefix, defaults in CONFIDENCE_FIELDS:
            key = confidence_environment_key(prefix, level)
            try:
                raw = environment.get(key)
            except Exception:
                raw = None
            fields[field] = _confidence_value(
                raw, key, defaults[level], warned,
            )
        thresholds[level] = fields
    return thresholds


def apply_confidence(environment=None) -> None:
    """Rebuild :data:`LEVELS` with the configured bars.

    Read once at import, and again only when something deliberately
    re-reads the environment. :func:`level_rule` consults ``LEVELS`` on every
    call, so a rebuilt table reaches every decision without any policy object
    being rebuilt.
    """
    thresholds = load_confidence(environment)
    for level, fields in thresholds.items():
        rule = LEVELS.get(level)
        if rule is not None:
            LEVELS[level] = replace(rule, **fields)


apply_confidence()


def level_rule(level: object) -> LevelRule:
    """Return the rule for ``level``, failing closed to strict when unknown."""
    if isinstance(level, str) and level in LEVELS:
        return LEVELS[level]
    if level in WITHDRAWN_LEVELS:
        LOGGER.warning(
            "plate matching level %r was withdrawn before release; using strict",
            level,
        )
    else:
        LOGGER.warning("unknown plate matching level %r; failing closed to strict", level)
    return LEVELS[LEVEL_STRICT]


@dataclass(frozen=True)
class Band:
    """A local-time window running ``[start_minute, end_minute)``.

    ``end_minute`` may be less than or equal to ``start_minute``, which means
    the band wraps past midnight (22:00-08:00).
    """

    start_minute: int
    end_minute: int
    level: str

    @property
    def label(self) -> str:
        return f"{_format_minute(self.start_minute)}-{_format_minute(self.end_minute)}"

    @property
    def wraps(self) -> bool:
        return self.end_minute <= self.start_minute

    @property
    def duration_minutes(self) -> int:
        if self.wraps:
            return MINUTES_PER_DAY - self.start_minute + self.end_minute
        return self.end_minute - self.start_minute

    def contains(self, minute_of_day: int) -> bool:
        if self.wraps:
            return minute_of_day >= self.start_minute or minute_of_day < self.end_minute
        return self.start_minute <= minute_of_day < self.end_minute

    def to_wire(self) -> dict[str, str]:
        return {
            "start": _format_minute(self.start_minute),
            "end": _format_minute(self.end_minute),
            "level": self.level,
        }


@dataclass(frozen=True)
class ResolvedPolicy:
    """The level in force at one instant, with the context an event records."""

    band: str
    level: str
    rule: LevelRule
    timezone_name: str
    local_time: str

    def to_wire(self) -> dict[str, object]:
        return {
            "band": self.band,
            "level": self.level,
            "timezone": self.timezone_name,
            "local_time": self.local_time,
        }


@dataclass(frozen=True)
class MatchPolicy:
    """A validated 24-hour schedule of fuzziness levels."""

    bands: tuple[Band, ...]
    timezone_name: str = DEFAULT_TIMEZONE

    def resolve(self, moment: datetime | None = None) -> ResolvedPolicy:
        """Return the level in force at ``moment``.

        ``moment`` should be timezone-aware. A naive datetime is interpreted as
        UTC — see :meth:`_local_time`.
        """
        local = self._local_time(moment)
        if local is None:
            # Without a trustworthy local clock the schedule cannot be
            # honoured, so take the strictest level the owner configured
            # rather than the most permissive one.
            level = _strictest_level(self.bands)
            LOGGER.warning(
                "plate matching schedule could not be resolved to local time; "
                "using the strictest configured level %r",
                level,
            )
            return ResolvedPolicy(
                band="unresolved",
                level=level,
                rule=level_rule(level),
                timezone_name=self.timezone_name,
                local_time="unknown",
            )
        minute_of_day = local.hour * 60 + local.minute
        for band in self.bands:
            if band.contains(minute_of_day):
                return ResolvedPolicy(
                    band=band.label,
                    level=band.level,
                    rule=level_rule(band.level),
                    timezone_name=self.timezone_name,
                    local_time=f"{local.hour:02d}:{local.minute:02d}",
                )
        # Construction guarantees full coverage; a miss means the policy was
        # built by hand around the validator, so fail closed.
        LOGGER.warning("plate matching schedule has no band covering %02d:%02d",
                       local.hour, local.minute)
        return ResolvedPolicy(
            band="uncovered",
            level=LEVEL_STRICT,
            rule=LEVELS[LEVEL_STRICT],
            timezone_name=self.timezone_name,
            local_time=f"{local.hour:02d}:{local.minute:02d}",
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "timezone": self.timezone_name,
            "bands": [band.to_wire() for band in self.bands],
        }

    def _local_time(self, moment: datetime | None) -> datetime | None:
        """Convert ``moment`` to site-local time.

        A naive datetime is read as **UTC**, never as local wall time. Reading
        it as local would be a silent hour of drift under Irish Summer Time,
        which is precisely the hour a 22:30 decision would be misfiled into
        the daytime band. Production passes an aware clock; this is the rule
        for anything that does not.
        """
        if moment is None:
            moment = datetime.now(timezone.utc)
        if not isinstance(moment, datetime):
            return None
        try:
            zone = ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            LOGGER.warning("timezone %r is unavailable on this host", self.timezone_name)
            return None
        try:
            if moment.tzinfo is None:
                LOGGER.debug("naive moment passed to the schedule; reading it as UTC")
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(zone)
        except (OverflowError, ValueError):
            return None


def _single_band_policy(level: str, timezone_name: str = DEFAULT_TIMEZONE) -> MatchPolicy:
    return MatchPolicy(
        bands=(Band(0, MINUTES_PER_DAY, level),), timezone_name=timezone_name
    )


#: What the controller does with no schedule configured: today's shipped
#: behaviour, unchanged, around the clock.
DEFAULT_POLICY = _single_band_policy(LEVEL_STANDARD)

#: Where an unusable schedule lands.
STRICT_POLICY = _single_band_policy(LEVEL_STRICT)

#: The schedule the app offers as its starting point. The controller never
#: applies this on its own; it is here so both sides agree on the wording.
RECOMMENDED_POLICY = MatchPolicy(
    bands=(
        Band(8 * 60, 22 * 60, LEVEL_STANDARD),
        Band(22 * 60, 8 * 60, LEVEL_STRICT),
    )
)


def parse_policy(payload: object) -> MatchPolicy:
    """Build a :class:`MatchPolicy` from a settings document.

    Raises :class:`MatchPolicyError` for anything that does not describe a
    complete, non-overlapping 24-hour schedule. An unknown *level* is not an
    error: it degrades to ``strict`` so a newer app can name a level this
    controller has never heard of without locking the gate open.
    """
    if not isinstance(payload, dict):
        raise MatchPolicyError("plate matching settings must be an object")
    version = payload.get("schema_version", SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise MatchPolicyError("plate matching schema_version must be an integer")
    if version > SCHEMA_VERSION:
        raise MatchPolicyError(
            f"plate matching schema_version {version} is newer than {SCHEMA_VERSION}"
        )
    timezone_name = payload.get("timezone", DEFAULT_TIMEZONE)
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise MatchPolicyError("plate matching timezone must be a non-empty string")
    timezone_name = timezone_name.strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as error:
        raise MatchPolicyError(f"unknown timezone {timezone_name!r}") from error

    raw_bands = payload.get("bands")
    if not isinstance(raw_bands, list) or not raw_bands:
        raise MatchPolicyError("plate matching schedule must list at least one band")
    if len(raw_bands) > MAX_BANDS:
        raise MatchPolicyError(f"plate matching schedule exceeds {MAX_BANDS} bands")

    bands = tuple(_parse_band(entry) for entry in raw_bands)
    _require_full_cover(bands)
    return MatchPolicy(bands=bands, timezone_name=timezone_name)


def safe_policy(payload: object) -> MatchPolicy:
    """Parse ``payload``, falling back to a fail-closed strict schedule.

    ``None`` means "no schedule configured" and keeps today's behaviour.
    """
    if payload is None:
        return DEFAULT_POLICY
    try:
        return parse_policy(payload)
    except MatchPolicyError as error:
        LOGGER.warning(
            "plate matching settings rejected (%s); failing closed to exact matches",
            error,
        )
        return STRICT_POLICY


def _parse_band(entry: object) -> Band:
    if not isinstance(entry, dict):
        raise MatchPolicyError("each schedule band must be an object")
    start = _parse_minute(entry.get("start"), "start")
    end = _parse_minute(entry.get("end"), "end")
    level = entry.get("level")
    if not isinstance(level, str) or not level:
        raise MatchPolicyError("each schedule band must name a level")
    if level not in LEVELS:
        LOGGER.warning(
            "schedule band %s-%s names %s level %r; failing closed to strict",
            _format_minute(start), _format_minute(end),
            "withdrawn" if level in WITHDRAWN_LEVELS else "unknown", level,
        )
        level = LEVEL_STRICT
    if start == end:
        raise MatchPolicyError(
            f"schedule band {_format_minute(start)}-{_format_minute(end)} is empty"
        )
    return Band(start_minute=start, end_minute=end, level=level)


def _parse_minute(value: object, field: str) -> int:
    if not isinstance(value, str):
        raise MatchPolicyError(f"schedule band {field} must be an 'HH:MM' string")
    text = value.strip()
    if text == "24:00":
        return MINUTES_PER_DAY
    if len(text) != 5 or text[2] != ":":
        raise MatchPolicyError(f"schedule band {field} must be an 'HH:MM' string")
    hours, minutes = text[:2], text[3:]
    if not hours.isdigit() or not minutes.isdigit():
        raise MatchPolicyError(f"schedule band {field} must be an 'HH:MM' string")
    hour, minute = int(hours), int(minutes)
    if hour > 23 or minute > 59:
        raise MatchPolicyError(f"schedule band {field} {text!r} is not a valid time")
    return hour * 60 + minute


def _require_full_cover(bands: tuple[Band, ...]) -> None:
    """Reject any schedule that leaves a minute uncovered or covered twice."""
    covered = [0] * MINUTES_PER_DAY
    for band in bands:
        minute = band.start_minute % MINUTES_PER_DAY
        for _ in range(band.duration_minutes):
            covered[minute] += 1
            minute = (minute + 1) % MINUTES_PER_DAY
    overlapping = next(
        (index for index, count in enumerate(covered) if count > 1), None
    )
    if overlapping is not None:
        raise MatchPolicyError(
            f"schedule bands overlap at {_format_minute(overlapping)}"
        )
    uncovered = next((index for index, count in enumerate(covered) if count == 0), None)
    if uncovered is not None:
        raise MatchPolicyError(
            f"schedule leaves {_format_minute(uncovered)} uncovered"
        )


def _strictest_level(bands: tuple[Band, ...]) -> str:
    levels = {band.level for band in bands}
    for level in LEVEL_ORDER:
        if level in levels:
            return level
    return LEVEL_STRICT


def _format_minute(minute: int) -> str:
    minute %= MINUTES_PER_DAY + 1
    if minute == MINUTES_PER_DAY:
        return "24:00"
    return f"{minute // 60:02d}:{minute % 60:02d}"
