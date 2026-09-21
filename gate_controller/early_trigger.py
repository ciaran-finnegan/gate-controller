"""Notice a vehicle before the camera says so, from a patch of the small stream.

After the re-aim of 2026-09-20 an arriving vehicle is in the picture for about
three seconds: it comes out from behind the foreground fence at the far left
and stops at the gate. On the one arrival measured since, the camera's own
vehicle alarm fired less than a second before the car stopped -- the camera's
detection latency ate two of the three seconds. This module watches the place
a vehicle *first* appears and says so as soon as it can.

**What it watches.** The camera's 640x360 sub stream, which MediaMTX already
pulls for the live view. One ffmpeg child decodes it, crops the far-lane patch
(``GATE_EARLY_TRIGGER_PATCH``, frame fractions like ``GATE_PLATE_REGION``) and
hands over 160 px grey pictures a few times a second. Measured on the Pi on
2026-09-21 that costs 1.8% of one core, against 4.8% for the 4K stream through
the hardware decoder at 2 fps and 56% for the 4K stream in software; see
``docs/early-trigger.md``.

**How it decides.** Each picture is averaged down to a 24x16 grid of cells and
compared with a slowly adapting background. By day the comparison is in the
log domain with the median change removed, so an exposure step or the sun
going in is no change at all, and what is left has to be one connected,
vehicle-sized blob that *enters* rather than appears everywhere at once and
holds for consecutive samples. By night -- the patch is black -- the signal is
a bright source that appears, persists and does not shrink, with diffuse light
(a car passing on the road beyond, the floodlight) removed the same way. The
two are separate detectors with separate thresholds, chosen by the measured
luma of the background.

**What it may do.** ``GATE_EARLY_TRIGGER=off|shadow|on``; the code's default is
``off``. In ``shadow`` it journals and records and touches nothing. In ``on`` a
would-trigger asks :class:`~gate_controller.trigger_capture.TriggerFrameCapture`
for a *local-only* sweep. The rule that sweep runs under is not in this module
and is not negotiable from it: nothing of an early-origin passage reaches the
cloud plate reader until the camera's own vehicle event for it has arrived.

Everything it sees is written to its own SQLite file beside the controller's
database (its own file so that a write here can never stand in front of the
relay's claim), with the evidence numbers, a small before/after picture kept
locally, and what the confirmation layers made of it.
"""
from __future__ import annotations

from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import subprocess
from threading import Lock, Thread
from time import monotonic, sleep

from .plate_region import PlateRegion
from .scene import thumbnail_difference

LOGGER = logging.getLogger(__name__)

MODE_OFF, MODE_SHADOW, MODE_ON = "off", "shadow", "on"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)
SOURCE_VISION, SOURCE_AUDIO, SOURCE_CAMERA = "vision", "audio", "camera"
KIND_WOULD_TRIGGER, KIND_CAMERA_ALARM = "would_trigger", "camera_alarm"
LIGHT_DAY, LIGHT_NIGHT = "day", "night"

ENV_MODE = "GATE_EARLY_TRIGGER"
ENV_PATCH = "GATE_EARLY_TRIGGER_PATCH"
ENV_SOURCE = "GATE_EARLY_TRIGGER_SOURCE_URL"
ENV_FPS = "GATE_EARLY_TRIGGER_FPS"
ENV_HOURS = "GATE_EARLY_TRIGGER_HOURS"
ENV_TIMEZONE = "GATE_EARLY_TRIGGER_TIMEZONE"
ENV_MIN_INTERVAL = "GATE_EARLY_TRIGGER_MIN_INTERVAL_SECONDS"
ENV_MAX_PER_HOUR = "GATE_EARLY_TRIGGER_MAX_PER_HOUR"
ENV_BACKOFF = "GATE_EARLY_TRIGGER_BACKOFF_SECONDS"
ENV_LAYERS_PER_MINUTE = "GATE_EARLY_TRIGGER_LAYERS_PER_MINUTE"
ENV_THUMBNAILS = "GATE_EARLY_TRIGGER_THUMBNAILS"
ENV_THUMBNAIL_MAX = "GATE_EARLY_TRIGGER_THUMBNAIL_MAX"
ENV_THUMBNAIL_DAYS = "GATE_EARLY_TRIGGER_THUMBNAIL_DAYS"

#: Where a vehicle first appears, with a margin. Measured after the re-aim:
#: first seen at x 0.05-0.15, y 0.30-0.45 with a 120-190 px plate, the first
#: readable plate centred at (0.19, 0.42). Laid over a live daytime frame
#: (2026-09-21 06:27 UTC) that is the bright gap where the drive comes in at
#: about (0.09, 0.37) and the gravel below and to the right of it. The box
#: runs from the foreground fence (x 0.02) to 0.34, so the first second of
#: travel along the lane stays inside, and from y 0.26 to 0.58: down to the
#: gravel, which is the steadiest background in the picture, and only as far
#: up as the top of the far fence, because everything above that is trees,
#: which are the least steady. It clears the camera's clock and watermark.
DEFAULT_PATCH = "0.02,0.26,0.32,0.32"
DEFAULT_SOURCE_URL = "rtsp://127.0.0.1:8554/camera"
#: The sub stream is decoded in full whatever is sampled from it (its GOP is
#: 4 s, so keyframes alone would be one picture every four seconds), and the
#: decode is the cost: 1.8% of a core at 2 fps, 1.9% at 5. So the rate is set
#: by latency instead. Two consecutive samples confirm a daytime vehicle;
#: at 2 fps that is up to a second after it appears, half of the two seconds
#: being fought for, and at 4 fps it is half a second.
DEFAULT_FPS = 4.0
DEFAULT_TIMEZONE = "Europe/Dublin"
DEFAULT_HOURS = "00:00-24:00"
THUMBNAIL_WIDTH = 160
GRID_WIDTH, GRID_HEIGHT = 24, 16
#: The sub stream's GOP was measured at 4 s (two keyframes in 82 frames at
#: 10 fps). A decoder that joins mid-GOP shows rubbish until the next
#: keyframe, so nothing is looked at for this long after the child starts.
SETTLE_SECONDS = 5.0
#: After a pause the background is still known, so it only has to catch up
#: with whatever moved while nobody was looking.
RESUME_SECONDS = 2.0
#: The camera cannot push a second webhook within 20 s, so nothing is lost by
#: not asking for a second early sweep inside it either.
DEFAULT_MIN_INTERVAL_SECONDS = 20.0
#: Unconfirmed early sweeps allowed in any hour before the feature stands
#: itself down. A quick-aborted sweep costs about 1.5 core-seconds (decoder
#: start plus a second of 5 fps hardware decode) and one that runs its whole
#: six seconds about 9; twelve of the worst kind is 108 core-seconds an hour,
#: 3% of one core, and no cloud lookup at all, by construction.
DEFAULT_MAX_PER_HOUR = 12
DEFAULT_BACKOFF_SECONDS = 1800.0
MAX_BACKOFF_SECONDS = 4 * 3600.0
DEFAULT_LAYERS_PER_MINUTE = 4
DEFAULT_THUMBNAIL_MAX = 500
DEFAULT_THUMBNAIL_DAYS = 7.0
#: A would-trigger with no camera alarm within this long either side of it is
#: a false trigger.
CORRELATION_SECONDS = 60.0
#: A camera alarm with no would-trigger in this long before it was missed.
#: It is the longest an unconfirmed early sweep runs, so an earlier
#: would-trigger than this bought the passage nothing.
USEFUL_LEAD_SECONDS = 6.0
GOOD_READ_SCORE = 0.75
CORRELATE_EVERY_SECONDS = 60.0
STATUS_EVERY_SECONDS = 600.0


@dataclass(frozen=True)
class DetectorConfig:
    """Two detectors' thresholds.

    The ones a shadow day is expected to move are settable from the
    environment (``load_config``: the rate, the day delta, areas, scatter and
    persistence, and every night threshold but the hysteresis and the largest
    area). The rest -- the global-jump bound, the largest areas, the noise
    multiplier and the time constants -- are code defaults, and changing one
    is a change to this file.
    """

    fps: float = DEFAULT_FPS
    # -- day: log-domain contrast against the background -------------------
    #: |log ratio| a cell must move by, after the median is removed. 0.18 is a
    #: 20% change in brightness, about what a car body is against gravel.
    day_delta: float = 0.18
    #: The connected blob as a share of the patch.
    day_min_area: float = 0.06
    day_max_area: float = 0.80
    #: A vehicle *enters*. The stored frames show a car's front standing the
    #: full height of this patch, and at the measured 0.18 frame-widths a second
    #: it takes another 14% of the patch each sample (23% for one going half as
    #: fast again), so the first time its blob is seen it is under 30%. Shade
    #: arriving when the sun comes out is not bound by that.
    day_entry_max_area: float = 0.45
    #: Changed cells outside the blob. Foliage, rain and dappled shade change
    #: cells all over the patch; a vehicle changes one region.
    day_max_scatter: float = 0.12
    day_persistence: int = 2
    #: |median log ratio| that means the whole patch changed brightness: an
    #: exposure step or the sun. The background is re-based, nothing triggers.
    global_jump: float = 0.22
    # -- night: a bright source on a black patch -----------------------------
    #: Background mean (0-255) below which the patch is dark. The re-aimed
    #: view at night measured 0.1-1.8; a dull dawn is over 40.
    night_luma: float = 28.0
    night_luma_hysteresis: float = 8.0
    #: How much brighter than the background a cell must be, in levels, once
    #: the diffuse light (the median rise) is taken off.
    night_delta: float = 60.0
    #: ...and how bright in itself. A lamp in view blooms to near white.
    night_peak: float = 150.0
    night_min_cells: int = 2
    night_max_area: float = 0.60
    #: Samples the source must hold for. A passing car's beam crosses in well
    #: under a second; four samples at 4 fps span 0.75 s.
    night_persistence: int = 4
    #: The source must not be fading: its blob at the last sample at least
    #: this share of its first.
    night_min_growth: float = 0.8
    #: How fast the source may cross the patch, in patch widths a second. A
    #: vehicle coming up the lane was measured crossing the frame at about
    #: 0.18 of its width a second, which is 0.6 of this patch; a beam that
    #: crosses the whole patch inside a second is a car going past.
    night_max_speed: float = 0.9
    # -- shared --------------------------------------------------------------
    noise_k: float = 3.5
    background_seconds: float = 10.0
    #: A thing that stays is part of the scene: a car parked in the patch is
    #: absorbed in about this long and stops holding the detector occupied.
    absorb_seconds: float = 90.0
    noise_seconds: float = 20.0
    warmup_seconds: float = 8.0
    rearm_seconds: float = 3.0
    refractory_seconds: float = 10.0


@dataclass(frozen=True)
class EarlyTriggerConfig:
    mode: str = MODE_OFF
    patch: PlateRegion = field(default_factory=lambda: _parse_patch(DEFAULT_PATCH))
    source_url: str = DEFAULT_SOURCE_URL
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    hours_text: str = DEFAULT_HOURS
    timezone_name: str = DEFAULT_TIMEZONE
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    max_per_hour: int = DEFAULT_MAX_PER_HOUR
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS
    layers_per_minute: int = DEFAULT_LAYERS_PER_MINUTE
    thumbnails: bool = True
    thumbnail_max: int = DEFAULT_THUMBNAIL_MAX
    thumbnail_days: float = DEFAULT_THUMBNAIL_DAYS
    state_directory: Path | None = None
    #: What the operator asked for, when it was not one of the three states.
    requested_mode: str | None = None

    @property
    def enabled(self) -> bool:
        return self.mode in (MODE_SHADOW, MODE_ON)

    @property
    def frame_size(self) -> tuple[int, int]:
        """The grey picture ffmpeg hands over, from the patch's own shape."""
        aspect = (self.patch.height * 9.0) / (self.patch.width * 16.0)
        height = max(2, int(round(THUMBNAIL_WIDTH * aspect / 2.0)) * 2)
        return THUMBNAIL_WIDTH, height


def _parse_patch(text: str) -> PlateRegion:
    parts = [part.strip() for part in str(text).split(",")]
    if len(parts) != 4:
        raise ValueError(f"{ENV_PATCH} must be four comma-separated fractions: x,y,w,h")
    return PlateRegion(*(float(part) for part in parts))


def load_mode(environment=None) -> str:
    return load_config(environment).mode


def load_config(environment=None, state_directory: Path | None = None) -> EarlyTriggerConfig:
    """Read the environment. Never raises: a bad value is journalled and defaulted.

    An unknown state is ``off``. That is the one direction a typo may fail in:
    this is a feature that can start work on its own, and a misspelt ``shadow``
    must not turn into ``on``.
    """
    environment = os.environ if environment is None else environment
    raw = str(environment.get(ENV_MODE) or "").strip().lower()
    mode = raw if raw in MODES else MODE_OFF
    requested = None
    if raw and raw not in MODES:
        requested = raw
        LOGGER.warning("gate_early_trigger %s=%r status=rejected using=off", ENV_MODE, raw)
    try:
        patch = _parse_patch(environment.get(ENV_PATCH) or DEFAULT_PATCH)
    except (TypeError, ValueError) as error:
        LOGGER.warning("gate_early_trigger %s status=rejected using=%s detail=%s",
                       ENV_PATCH, DEFAULT_PATCH, error)
        patch = _parse_patch(DEFAULT_PATCH)
    defaults = DetectorConfig()

    def number(name, default, low, high):
        return _bounded(environment, f"GATE_EARLY_TRIGGER_{name}", default, low, high)

    detector = DetectorConfig(
        fps=_bounded(environment, ENV_FPS, defaults.fps, 1.0, 10.0),
        day_delta=number("DAY_DELTA", defaults.day_delta, 0.05, 1.5),
        day_min_area=number("DAY_MIN_AREA", defaults.day_min_area, 0.01, 0.5),
        day_entry_max_area=number("DAY_ENTRY_MAX_AREA", defaults.day_entry_max_area, 0.05, 1.0),
        day_max_scatter=number("DAY_MAX_SCATTER", defaults.day_max_scatter, 0.0, 1.0),
        day_persistence=int(number("DAY_PERSISTENCE", defaults.day_persistence, 1, 20)),
        night_luma=number("NIGHT_LUMA", defaults.night_luma, 0.0, 255.0),
        night_delta=number("NIGHT_DELTA", defaults.night_delta, 5.0, 255.0),
        night_peak=number("NIGHT_PEAK", defaults.night_peak, 0.0, 255.0),
        night_min_cells=int(number("NIGHT_MIN_CELLS", defaults.night_min_cells, 1, 200)),
        night_persistence=int(number("NIGHT_PERSISTENCE", defaults.night_persistence, 1, 40)),
        night_min_growth=number("NIGHT_MIN_GROWTH", defaults.night_min_growth, 0.0, 10.0),
        night_max_speed=number("NIGHT_MAX_SPEED", defaults.night_max_speed, 0.05, 50.0),
    )
    hours = str(environment.get(ENV_HOURS) or "").strip() or DEFAULT_HOURS
    zone = str(environment.get(ENV_TIMEZONE) or "").strip() or DEFAULT_TIMEZONE
    try:
        _hours(hours, zone)
    except Exception:
        LOGGER.warning("gate_early_trigger %s=%r %s=%r status=rejected using=%s",
                       ENV_HOURS, hours, ENV_TIMEZONE, zone, DEFAULT_HOURS)
        hours, zone = DEFAULT_HOURS, DEFAULT_TIMEZONE
    return EarlyTriggerConfig(
        mode=mode, patch=patch,
        source_url=str(environment.get(ENV_SOURCE) or "").strip() or DEFAULT_SOURCE_URL,
        detector=detector, hours_text=hours, timezone_name=zone,
        min_interval_seconds=_bounded(
            environment, ENV_MIN_INTERVAL, DEFAULT_MIN_INTERVAL_SECONDS, 5.0, 3600.0),
        max_per_hour=int(_bounded(environment, ENV_MAX_PER_HOUR, DEFAULT_MAX_PER_HOUR, 1, 120)),
        backoff_seconds=_bounded(
            environment, ENV_BACKOFF, DEFAULT_BACKOFF_SECONDS, 60.0, MAX_BACKOFF_SECONDS),
        layers_per_minute=int(_bounded(
            environment, ENV_LAYERS_PER_MINUTE, DEFAULT_LAYERS_PER_MINUTE, 0, 30)),
        thumbnails=str(environment.get(ENV_THUMBNAILS) or "true").strip().lower()
        not in ("0", "false", "no", "off"),
        thumbnail_max=int(_bounded(environment, ENV_THUMBNAIL_MAX, DEFAULT_THUMBNAIL_MAX, 0, 5000)),
        thumbnail_days=_bounded(environment, ENV_THUMBNAIL_DAYS, DEFAULT_THUMBNAIL_DAYS, 0.1, 60.0),
        state_directory=Path(state_directory) if state_directory is not None else None,
        requested_mode=requested,
    )


def _bounded(environment, name: str, default, low, high):
    raw = environment.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or not low <= value <= high:
        LOGGER.warning("gate_early_trigger %s=%r status=rejected using=%s", name, raw, default)
        return default
    return value


def _hours(text: str, zone: str):
    # The very class the farm-machinery policy parses its admit hours with, so
    # the two settings cannot drift apart in format.
    from .agricultural import Hours
    return Hours.parse(text, zone)


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Observation:
    """What one sample showed. ``features`` is what gets journalled and stored."""

    verdict: str
    triggered: bool
    light: str
    features: dict


class PatchDetector:
    """Day and night detectors over a small grid of cell means. Pure Python.

    Feed it ``GRID_WIDTH * GRID_HEIGHT`` cell means (0-255) and a monotonic
    time. It answers with a verdict for the sample:

    ``warming``   too soon after a start or a re-base to say anything
    ``clear``     nothing there
    ``global``    the whole patch changed brightness; the background is re-based
    ``sudden``    a large region changed at once: light, not a vehicle entering
    ``scattered`` change all over the patch: foliage, rain, dappled shade
    ``candidate`` something vehicle-like, not yet held for long enough
    ``fading``    (night) a source that is already going away
    ``sweeping``  (night) a source crossing the patch too fast to be on the lane
    ``trigger``   a would-trigger, once per appearance
    ``occupied``  still the thing that triggered, or the refractory gap
    """

    def __init__(self, config: DetectorConfig | None = None, *,
                 width: int = GRID_WIDTH, height: int = GRID_HEIGHT):
        self.config = config or DetectorConfig()
        self.width, self.height = width, height
        self._cells = width * height
        self._background: list[float] | None = None
        self._noise: list[float] = []
        self._previous: list[int] | None = None
        self._last_at: float | None = None
        self._armed_at: float | None = None
        self._light = LIGHT_DAY
        self._run = 0
        self._run_first: dict | None = None
        self._occupied = False
        self._clear_since: float | None = None
        self._last_trigger_at: float | None = None
        self._fast_until: float | None = None
        self._last: Observation | None = None

    @property
    def last(self) -> Observation | None:
        return self._last

    @property
    def light(self) -> str:
        return self._light

    def rebase(self, now: float, seconds: float | None = None) -> None:
        """Take whatever is there at ``now`` as the scene. After a pause or a restart.

        The background and the learnt noise are kept; for ``seconds`` (the
        whole warm-up when not given) the background follows the picture
        quickly and nothing can trigger.
        """
        self._armed_at = now + (self.config.warmup_seconds if seconds is None else seconds)
        self._fast_until = self._armed_at
        self._run, self._run_first = 0, None
        self._previous = None
        self._last_at = None

    def observe(self, cells, now: float) -> Observation:
        cells = list(cells)
        if len(cells) != self._cells:
            raise ValueError("a sample must carry one value per grid cell")
        config = self.config
        if self._background is None:
            self._background = [float(value) for value in cells]
            self._noise = [0.0] * self._cells
            self._armed_at = now + config.warmup_seconds
            self._fast_until = self._armed_at
        dt = 1.0 / config.fps if self._last_at is None else min(2.0, max(0.0, now - self._last_at))
        self._last_at = now
        background = self._background
        bg_mean = sum(background) / self._cells
        mean = sum(cells) / self._cells
        self._choose_light(bg_mean)
        night = self._light == LIGHT_NIGHT
        if night:
            residual = [value - base for value, base in zip(cells, background)]
            floor = config.night_delta
        else:
            residual = [
                math.log((value + 16.0) / (base + 16.0)) for value, base in zip(cells, background)
            ]
            floor = config.day_delta
        shift = _median(residual)
        residual = [value - shift for value in residual]
        noise = self._noise
        if night:
            changed = [
                index for index, value in enumerate(residual)
                if value > max(floor, config.noise_k * noise[index])
                and cells[index] >= config.night_peak
            ]
        else:
            changed = [
                index for index, value in enumerate(residual)
                if abs(value) > max(floor, config.noise_k * noise[index])
            ]
        blob = _largest_blob(changed, self.width, self.height)
        blob_fraction = len(blob) / self._cells
        changed_fraction = len(changed) / self._cells
        cx, cy = _centroid(blob, self.width, self.height)
        features = {
            "light": self._light,
            "mean_luma": round(mean, 1),
            "bg_luma": round(bg_mean, 1),
            "luma_jump": round(mean - bg_mean, 1),
            "peak": max(cells),
            "shift": round(shift, 3),
            "changed_fraction": round(changed_fraction, 4),
            "blob_fraction": round(blob_fraction, 4),
            "blob_cells": len(blob),
            "scatter": round(changed_fraction - blob_fraction, 4),
            "cx": cx, "cy": cy,
            "scene_difference": round(
                thumbnail_difference([int(round(v)) for v in background], cells), 4),
            "stillness": (
                None if self._previous is None
                else round(thumbnail_difference(self._previous, cells), 4)
            ),
        }
        self._previous = cells
        verdict = self._judge(features, blob, shift, now)
        features["persistence"] = self._run
        first = self._run_first
        if first is not None and blob:
            features["track_dx"] = round(cx - first["cx"], 3)
            features["track_dy"] = round(cy - first["cy"], 3)
            features["growth"] = round(len(blob) / max(1, first["cells"]), 2)
        features["verdict"] = verdict
        self._adapt(cells, residual, set(changed), verdict, dt, floor)
        observation = Observation(verdict, verdict == "trigger", self._light, features)
        self._last = observation
        return observation

    # -- the rules ----------------------------------------------------------
    def _judge(self, features: dict, blob: list[int], shift: float, now: float) -> str:
        config = self.config
        night = self._light == LIGHT_NIGHT
        if self._armed_at is not None and now < self._armed_at:
            self._run, self._run_first = 0, None
            return "warming"
        if not night and abs(shift) > config.global_jump:
            # The whole patch moved together: exposure, or the sun. Re-base.
            self._run, self._run_first = 0, None
            self._fast_until = now + 1.5
            return "global"
        fraction = features["blob_fraction"]
        if night:
            candidate = (
                len(blob) >= config.night_min_cells and fraction <= config.night_max_area
            )
        else:
            candidate = config.day_min_area <= fraction <= config.day_max_area
        if not candidate:
            self._run, self._run_first = 0, None
            if fraction > (config.night_max_area if night else config.day_max_area):
                self._fast_until = now + 1.5
                return "global"
            if self._clear_since is None:
                self._clear_since = now
            if self._occupied and now - self._clear_since >= config.rearm_seconds:
                self._occupied = False
            return "clear"
        self._clear_since = None
        if not night and features["scatter"] > config.day_max_scatter:
            self._run, self._run_first = 0, None
            return "scattered"
        if self._occupied:
            return "occupied"
        if self._run == 0:
            if not night and fraction > config.day_entry_max_area:
                # Too much, too soon: a vehicle enters, light arrives. Treat it
                # as the scene having changed and do not wait on it.
                self._fast_until = now + 1.5
                return "sudden"
            self._run_first = {
                "cx": features["cx"], "cy": features["cy"], "cells": len(blob), "at": now,
            }
        self._run += 1
        needed = config.night_persistence if night else config.day_persistence
        if self._run < needed:
            return "candidate"
        if night and len(blob) < config.night_min_growth * max(1, self._run_first["cells"]):
            return "fading"
        if night:
            elapsed = max(1e-3, now - self._run_first["at"])
            speed = abs(features["cx"] - self._run_first["cx"]) / elapsed
            features["speed"] = round(speed, 3)
            if speed > config.night_max_speed:
                return "sweeping"
        if (
            self._last_trigger_at is not None
            and now - self._last_trigger_at < config.refractory_seconds
        ):
            self._occupied = True
            return "occupied"
        self._occupied = True
        self._last_trigger_at = now
        return "trigger"

    def _choose_light(self, bg_mean: float) -> None:
        config = self.config
        if self._light == LIGHT_DAY and bg_mean < config.night_luma:
            switched = LIGHT_NIGHT
        elif self._light == LIGHT_NIGHT and bg_mean > config.night_luma + config.night_luma_hysteresis:
            switched = LIGHT_DAY
        else:
            return
        # The two detectors measure in different units, so the learnt noise of
        # one means nothing to the other.
        self._light = switched
        self._noise = [0.0] * self._cells
        self._run, self._run_first = 0, None

    def _adapt(self, cells, residual, changed: set, verdict: str, dt: float, floor: float) -> None:
        config = self.config
        fast = self._fast_until is not None and self._last_at is not None and self._last_at < self._fast_until
        quick = 0.5 if fast or verdict in ("global", "sudden") else min(1.0, dt / config.background_seconds)
        slow = min(1.0, dt / config.absorb_seconds)
        # While warming up the noise is learnt quickly, so that a hedge in the
        # wind is known for what it is before anything is allowed to trigger.
        learn = 0.2 if verdict == "warming" else min(1.0, dt / config.noise_seconds)
        cap = 3.0 * floor
        background, noise = self._background, self._noise
        for index, value in enumerate(cells):
            rate = slow if (index in changed and not fast) else quick
            background[index] += rate * (value - background[index])
            if verdict not in ("global", "sudden"):
                noise[index] += learn * (min(cap, abs(residual[index])) - noise[index])


def _median(values) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _largest_blob(changed, width: int, height: int) -> list[int]:
    """The largest 4-connected group among the changed cells."""
    remaining = set(changed)
    best: list[int] = []
    while remaining:
        start = remaining.pop()
        group, frontier = [start], [start]
        while frontier:
            cell = frontier.pop()
            x, y = cell % width, cell // width
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < width and 0 <= ny < height:
                    neighbour = ny * width + nx
                    if neighbour in remaining:
                        remaining.discard(neighbour)
                        group.append(neighbour)
                        frontier.append(neighbour)
        if len(group) > len(best):
            best = group
    return best


def _centroid(blob, width: int, height: int) -> tuple[float | None, float | None]:
    if not blob:
        return None, None
    x = sum(cell % width for cell in blob) / len(blob)
    y = sum(cell // width for cell in blob) / len(blob)
    return round((x + 0.5) / width, 3), round((y + 0.5) / height, 3)


def grid_from_frame(frame: bytes, size: tuple[int, int],
                    grid: tuple[int, int] = (GRID_WIDTH, GRID_HEIGHT)) -> list[int]:
    """Average a raw grey picture down to the grid. Pillow's box filter, in C."""
    from PIL import Image

    image = Image.frombytes("L", size, frame)
    return list(image.resize(grid, Image.Resampling.BOX).tobytes())


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS early_trigger_observations (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        at TEXT NOT NULL,
        at_epoch REAL NOT NULL,
        mode TEXT NOT NULL,
        light TEXT,
        detector_state TEXT,
        features TEXT NOT NULL DEFAULT '{}',
        action TEXT NOT NULL DEFAULT 'none',
        layers TEXT NOT NULL DEFAULT '{}',
        sweep TEXT,
        thumbnail TEXT,
        verdict TEXT NOT NULL DEFAULT 'pending',
        matched_id INTEGER,
        camera_alarm_at TEXT,
        lead_seconds REAL,
        first_read_at TEXT,
        first_read_score REAL,
        first_read_event_id INTEGER,
        correlated_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS early_trigger_observations_at"
    " ON early_trigger_observations (at_epoch)",
)


def ensure_schema(connection) -> None:
    with connection:
        for statement in SCHEMA:
            connection.execute(statement)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")


class EarlyTriggerStore:
    """The early trigger's own SQLite file. Every method is best effort.

    Its own file, beside the controller's database and not inside it: a write
    here is made from a background thread the moment something moves in the
    patch, and the controller's database is where the relay's claim takes its
    lock. Nothing the early trigger records may ever stand in front of that.
    """

    def __init__(self, path: Path, events_database: Path | None = None):
        self.path = Path(path)
        self.events_database = Path(events_database) if events_database else None
        self._lock = Lock()
        self._ready = False

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        if not self._ready:
            ensure_schema(connection)
            self._ready = True
        return connection

    def record(self, *, kind: str, source: str, at_epoch: float, mode: str,
               light: str | None = None, detector_state: str | None = None,
               features: dict | None = None, action: str = "none",
               thumbnail: str | None = None) -> int | None:
        try:
            with self._lock, closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    "INSERT INTO early_trigger_observations (kind, source, at, at_epoch, mode,"
                    " light, detector_state, features, action, thumbnail)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (kind, source, _iso(at_epoch), at_epoch, mode, light, detector_state,
                     json.dumps(features or {}, sort_keys=True), action, thumbnail),
                )
                return cursor.lastrowid
        except Exception:
            LOGGER.warning("gate_early_trigger stage=record_failed", exc_info=True)
            return None

    def annotate(self, row_id: int | None, **columns) -> None:
        """Set ``layers``, ``sweep`` or ``action`` on a row already written."""
        if row_id is None:
            return
        pairs = [(name, value) for name, value in columns.items()
                 if name in ("layers", "sweep", "action")]
        if not pairs:
            return
        try:
            with self._lock, closing(self._connect()) as connection, connection:
                for name, value in pairs:
                    if not isinstance(value, str):
                        value = json.dumps(value, sort_keys=True)
                    connection.execute(
                        f"UPDATE early_trigger_observations SET {name} = ? WHERE id = ?",
                        (value, row_id),
                    )
        except Exception:
            LOGGER.warning("gate_early_trigger stage=annotate_failed", exc_info=True)

    def correlate(self, now_epoch: float) -> int:
        """Pair would-triggers with camera alarms once the window has closed."""
        try:
            with self._lock, closing(self._connect()) as connection:
                return correlate(connection, now_epoch, events_database=self.events_database)
        except Exception:
            LOGGER.warning("gate_early_trigger stage=correlate_failed", exc_info=True)
            return 0


def correlate(connection, now_epoch: float, *, events_database: Path | None = None) -> int:
    """Settle every row old enough that its window has closed. Returns rows settled.

    A would-trigger is ``true`` when a camera alarm lies within
    ``CORRELATION_SECONDS`` of it and ``false`` when none does. A camera alarm
    is ``led`` when a would-trigger came in the ``USEFUL_LEAD_SECONDS`` before
    it, ``late_lead`` when one came earlier than that (too early to have
    helped), and ``missed`` when none came at all.
    """
    ensure_schema(connection)
    rows = connection.execute(
        "SELECT id, kind, at_epoch FROM early_trigger_observations"
        " WHERE verdict = 'pending' AND at_epoch <= ? ORDER BY at_epoch",
        (now_epoch - CORRELATION_SECONDS - 5.0,),
    ).fetchall()
    settled = 0
    for row_id, kind, at_epoch in rows:
        other = KIND_CAMERA_ALARM if kind == KIND_WOULD_TRIGGER else KIND_WOULD_TRIGGER
        if kind == KIND_WOULD_TRIGGER:
            low, high = at_epoch - 5.0, at_epoch + CORRELATION_SECONDS
        else:
            low, high = at_epoch - CORRELATION_SECONDS, at_epoch
        match = connection.execute(
            "SELECT id, at, at_epoch FROM early_trigger_observations WHERE kind = ?"
            " AND at_epoch BETWEEN ? AND ? ORDER BY ABS(at_epoch - ?) LIMIT 1",
            (other, low, high, at_epoch),
        ).fetchone()
        if kind == KIND_WOULD_TRIGGER:
            alarm_epoch = match[2] if match else None
            verdict = "true" if match else "false"
            lead = None if match is None else round(alarm_epoch - at_epoch, 3)
        else:
            alarm_epoch = at_epoch
            lead = None if match is None else round(at_epoch - match[2], 3)
            if match is None:
                verdict = "missed"
            elif 0.0 <= lead <= USEFUL_LEAD_SECONDS:
                verdict = "led"
            else:
                verdict = "late_lead"
        read = (
            _first_good_read(events_database, alarm_epoch)
            if alarm_epoch is not None else None
        )
        with connection:
            connection.execute(
                "UPDATE early_trigger_observations SET verdict = ?, matched_id = ?,"
                " camera_alarm_at = ?, lead_seconds = ?, first_read_at = ?,"
                " first_read_score = ?, first_read_event_id = ?, correlated_at = ?"
                " WHERE id = ?",
                (verdict, match[0] if match else None,
                 None if alarm_epoch is None else _iso(alarm_epoch), lead,
                 read[0] if read else None, read[1] if read else None,
                 read[2] if read else None, _iso(now_epoch), row_id),
            )
        settled += 1
    return settled


def _first_good_read(events_database: Path | None, alarm_epoch: float):
    """``(received_at, score, event_id)`` of the passage's first local read over the bar.

    Read from the controller's own database, opened read-only. The passage is
    taken as ten seconds before the alarm to sixty after it.
    """
    if events_database is None or not Path(events_database).exists():
        return None
    # The controller writes `received_at` with microseconds (or with no
    # fraction at all) and `_iso` writes milliseconds, and SQLite compares the
    # two as text. So the text window is a second wider than the real one, and
    # the real one is applied to the parsed times below.
    first, last = alarm_epoch - 10.0, alarm_epoch + CORRELATION_SECONDS
    low, high = _iso(first - 1.0), _iso(last + 1.0)
    try:
        with closing(sqlite3.connect(f"file:{events_database}?mode=ro", uri=True, timeout=2.0)) as events:
            rows = events.execute(
                "SELECT e.id, e.received_at, t.payload FROM events e"
                " JOIN event_telemetry t ON t.event_id = e.id"
                " WHERE e.received_at BETWEEN ? AND ? ORDER BY e.received_at, e.id",
                (low, high),
            ).fetchall()
    except sqlite3.Error:
        return None
    for event_id, received_at, payload in rows:
        try:
            moment = datetime.fromisoformat(str(received_at))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            if not first <= moment.timestamp() <= last:
                continue
            local = (json.loads(payload) or {}).get("local_ocr") or {}
            score = float(local.get("score") or 0.0)
        except (TypeError, ValueError):
            continue
        if local.get("status") == "recognized" and local.get("plate") and score >= GOOD_READ_SCORE:
            return received_at, round(score, 3), event_id
    return None


class ThumbnailStore:
    """Small before/after pictures of the patch, kept locally and bounded.

    Two 160 px grey pictures side by side, JPEG quality 70: about 6 KB each.
    At most ``max_files`` of them and none older than ``max_days``, so the
    default bound is 500 x ~6 KB = about 3 MB. They go nowhere: not the
    corpus, not the dashboard.
    """

    def __init__(self, directory: Path, *, max_files: int, max_days: float):
        self.directory = Path(directory)
        self.max_files = max_files
        self.max_days = max_days

    def save(self, before: bytes | None, after: bytes, size: tuple[int, int],
             at_epoch: float, label: str) -> str | None:
        if self.max_files <= 0:
            return None
        try:
            from PIL import Image

            width, height = size
            sheet = Image.new("L", (width * 2, height))
            if before is not None:
                sheet.paste(Image.frombytes("L", size, before), (0, 0))
            sheet.paste(Image.frombytes("L", size, after), (width, 0))
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = datetime.fromtimestamp(at_epoch, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            name = f"{stamp}-{label}.jpg"
            output = BytesIO()
            sheet.save(output, format="JPEG", quality=70)
            (self.directory / name).write_bytes(output.getvalue())
            self.prune(at_epoch)
            return name
        except Exception:
            LOGGER.warning("gate_early_trigger stage=thumbnail_failed", exc_info=True)
            return None

    def prune(self, now_epoch: float) -> None:
        try:
            files = sorted(self.directory.glob("*.jpg"))
        except OSError:
            return
        cutoff = now_epoch - self.max_days * 86400.0
        excess = max(0, len(files) - self.max_files)
        for index, path in enumerate(files):
            try:
                if index < excess or path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue


# ---------------------------------------------------------------------------
# Confirmation layers, evaluated in shadow
# ---------------------------------------------------------------------------

class ConfirmationLayers:
    """What a second look would have said, recorded beside the would-trigger.

    Each layer is timed, bounded, and skipped -- and recorded as skipped --
    when the controller is busy with a vehicle. Both run on their own thread,
    one would-trigger at a time, and never more than ``per_minute`` a minute.

    ``clip_look`` takes a JPEG and returns label shares (the farm-machinery
    tower's prompts include an empty drive); ``lane_frame`` supplies that JPEG:
    a square of the clear stream around the lane, because the tower looks at
    the centre square of what it is given and an arrival first appears at the
    far left. ``plate_frame`` and ``plate_read`` are the clear stream's newest
    keyframe cut to the plate band and the sweep's own local reader.
    """

    PLATE_LOOKS = 3
    PLATE_LOOK_SPACING_SECONDS = 0.4

    def __init__(self, *, clip_look=None, lane_frame=None, plate_frame=None, plate_read=None,
                 busy=None, per_minute: int = DEFAULT_LAYERS_PER_MINUTE, clock=monotonic,
                 sleep=None):
        self._clip_look = clip_look
        self._lane_frame = lane_frame
        self._plate_frame = plate_frame
        self._plate_read = plate_read
        self._busy = busy or (lambda: None)
        self._per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._begun: deque = deque()
        self._running = Lock()

    def set_clip_look(self, clip_look) -> None:
        self._clip_look = clip_look

    def begin(self, done) -> bool:
        """Start the looks on their own thread; ``done(layers)`` gets the answer."""
        skipped = self._refusal()
        if skipped is not None:
            done({"clip": {"status": skipped}, "plate_look": {"status": skipped}})
            return False

        def run():
            try:
                done(self.evaluate())
            except Exception:
                LOGGER.warning("gate_early_trigger stage=layers_failed", exc_info=True)
            finally:
                self._running.release()

        try:
            Thread(target=run, name="gate-early-trigger-layers", daemon=True).start()
        except Exception:
            self._running.release()
            return False
        return True

    def _refusal(self) -> str | None:
        if self._per_minute <= 0:
            return "skipped_disabled"
        if self._busy():
            return "skipped_busy"
        if not self._running.acquire(blocking=False):
            return "skipped_running"
        now = self._clock()
        while self._begun and now - self._begun[0] >= 60.0:
            self._begun.popleft()
        if len(self._begun) >= self._per_minute:
            self._running.release()
            return "skipped_rate"
        self._begun.append(now)
        return None

    def evaluate(self) -> dict:
        return {"clip": self._clip(), "plate_look": self._plate()}

    def _clip(self) -> dict:
        if self._clip_look is None or self._lane_frame is None:
            return {"status": "unavailable"}
        if self._busy():
            return {"status": "skipped_busy"}
        started = self._clock()
        try:
            frame = self._lane_frame()
            if frame is None:
                return {"status": "no_frame"}
            answer = self._clip_look(frame)
        except Exception:
            return {"status": "error"}
        answer = dict(answer or {"status": "unavailable"})
        answer["ms"] = round((self._clock() - started) * 1000)
        return answer

    def _plate(self) -> dict:
        if self._plate_frame is None or self._plate_read is None:
            return {"status": "unavailable"}
        started = self._clock()
        reads = []
        for index in range(self.PLATE_LOOKS):
            if self._busy():
                # A real sweep owns the reader from here.
                return {"status": "skipped_busy", "reads": reads,
                        "ms": round((self._clock() - started) * 1000)}
            if index and self._sleep is not None:
                self._sleep(self.PLATE_LOOK_SPACING_SECONDS)
            try:
                frame = self._plate_frame()
                if frame is None:
                    reads.append({"status": "no_frame"})
                    continue
                read = self._plate_read(frame)
            except Exception:
                reads.append({"status": "error"})
                continue
            reads.append({
                "status": getattr(read, "status", "unavailable"),
                "plate_box": bool(getattr(read, "recognised", False)),
                "score": round(float(getattr(read, "score", 0.0) or 0.0), 3),
                "authorised": bool(getattr(read, "authorised", False)),
            })
        return {
            "status": "ok", "reads": reads,
            "plate_box": any(read.get("plate_box") for read in reads),
            "ms": round((self._clock() - started) * 1000),
        }


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

class EarlyTriggerWorker:
    """The background worker: one ffmpeg child, the detector, the record, the caps.

    ``capture`` is the production :class:`TriggerFrameCapture`. It is handed
    over in every mode and *called* only in ``on``: :meth:`_act` is the one
    place this module reaches anything that can start work, and in ``shadow``
    it returns before it gets there.
    """

    def __init__(self, config: EarlyTriggerConfig, *, capture=None, activity=None,
                 store: EarlyTriggerStore | None = None, layers: ConfirmationLayers | None = None,
                 thumbnails: ThumbnailStore | None = None, popen=subprocess.Popen,
                 clock=monotonic, wall_clock=None, detector: PatchDetector | None = None):
        self.config = config
        self._capture = capture
        self._activity = activity
        self._store = store
        self._layers = layers
        self._thumbnails = thumbnails
        self._popen = popen
        self._clock = clock
        self._wall = wall_clock or (lambda: datetime.now(timezone.utc).timestamp())
        self.detector = detector or PatchDetector(config.detector)
        self._hours = _hours(config.hours_text, config.timezone_name)
        self._process = None
        self._lock = Lock()
        self._closed = False
        self._frames: deque = deque(maxlen=max(2, int(config.detector.fps * 2) + 1))
        self._state = "starting"
        self._samples = 0
        self._would_triggers = 0
        self._scheduled = 0
        self._camera_alarms = 0
        self._restarts = 0
        self._pauses = 0
        self._last_acted_at: float | None = None
        self._unconfirmed: deque = deque()
        self._backoff_until: float | None = None
        self._backoff_seconds = config.backoff_seconds
        self._pending_sweep_row: int | None = None
        self._last_correlated_at = 0.0
        self._last_status_at = 0.0
        width, height = config.frame_size
        self.frame_size = (width, height)
        self.frame_bytes = width * height
        self.command = (
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-analyzeduration", "0", "-probesize", "32",
            "-threads", "1",
            "-i", config.source_url, "-map", "0:v:0", "-an",
            "-vf", ",".join((
                f"fps={config.detector.fps:g}", config.patch.ffmpeg_crop_filter(),
                f"scale={width}:{height}:flags=area", "format=gray",
            )),
            "-f", "rawvideo", "pipe:1",
        )
        self.child_environment = {"LANG": "C", "LC_ALL": "C"}
        if capture is not None:
            observe = getattr(capture, "set_early_observer", None)
            if callable(observe):
                observe(self.note_sweep)

    # -- what the rest of the controller tells it ---------------------------
    def set_clip_look(self, clip_look) -> None:
        """The image tower's ``look``, once the processor's policy has loaded it."""
        if self._layers is not None and callable(clip_look):
            self._layers.set_clip_look(clip_look)

    def note_camera_alarm(self, event=None) -> None:
        """The camera's own vehicle alarm arrived. Recorded with what we saw. Never raises."""
        try:
            self._camera_alarms += 1
            now = self._wall()
            last = self.detector.last
            features = dict(last.features) if last is not None else {}
            features["event_type"] = str(getattr(event, "event_type", "unknown"))
            thumbnail = self._thumbnail(now, "alarm")
            # A camera alarm settles the account for whatever came before it:
            # the feature is doing its job, so the backoff is forgiven.
            with self._lock:
                self._unconfirmed.clear()
                self._backoff_until = None
                self._backoff_seconds = self.config.backoff_seconds
            if self._store is not None:
                self._store.record(
                    kind=KIND_CAMERA_ALARM, source=SOURCE_CAMERA, at_epoch=now,
                    mode=self.config.mode, light=self.detector.light,
                    detector_state=self._state, features=features, thumbnail=thumbnail,
                )
        except Exception:
            LOGGER.warning("gate_early_trigger stage=alarm_note_failed", exc_info=True)

    def note_sweep(self, report: dict) -> None:
        """What became of the early sweep this worker asked for. Never raises."""
        try:
            row, self._pending_sweep_row = self._pending_sweep_row, None
            if self._store is not None and row is not None:
                self._store.annotate(row, sweep=report)
        except Exception:
            LOGGER.warning("gate_early_trigger stage=sweep_note_failed", exc_info=True)

    # -- the loop -------------------------------------------------------------
    def run_forever(self, stop_event) -> None:
        if not self.config.enabled:
            stop_event.wait()
            return
        LOGGER.info(
            "gate_early_trigger stage=configured mode=%s patch=%s fps=%g source=%s hours=%s "
            "min_interval_s=%g max_per_hour=%d%s",
            self.config.mode, self.config.patch.as_env(), self.config.detector.fps,
            "sub_stream", self.config.hours_text, self.config.min_interval_seconds,
            self.config.max_per_hour,
            "" if self.config.requested_mode is None
            else f" requested={self.config.requested_mode}",
        )
        while not stop_event.is_set() and not self._closed:
            if self._paused():
                self._state = "paused"
                stop_event.wait(0.25)
                continue
            try:
                self._watch(stop_event)
            except (OSError, ValueError):
                LOGGER.warning("gate_early_trigger stage=restart reason=stream_error")
            finally:
                self._stop_child()
            if not stop_event.is_set() and not self._paused():
                self._restarts += 1
                stop_event.wait(2.0)

    def _paused(self) -> bool:
        """A sweep or presence session has the frames, and the cores."""
        activity = self._activity
        try:
            if activity is not None and activity.busy_reason() is not None:
                return True
            active = getattr(self._capture, "session_active", None)
            return bool(active()) if callable(active) else False
        except Exception:
            return False

    def _watch(self, stop_event) -> None:
        process = self._popen(
            self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=self.child_environment, close_fds=True, bufsize=0,
        )
        self._process = process
        started = self._clock()
        self.detector.rebase(started + SETTLE_SECONDS, RESUME_SECONDS if self._samples else None)
        self._frames.clear()
        self._state = "settling"
        buffer = bytearray()
        while not stop_event.is_set() and not self._closed:
            if self._paused():
                self._pauses += 1
                last = self.detector.last
                LOGGER.info(
                    "gate_early_trigger stage=paused reason=session candidate=%s",
                    "yes" if last is not None and last.verdict == "candidate" else "no",
                )
                return
            chunk = _read_some(process.stdout, self.frame_bytes, timeout=0.5)
            if chunk is None:
                self._housekeeping()
                continue
            if not chunk:
                return
            buffer.extend(chunk)
            while len(buffer) >= self.frame_bytes:
                frame = bytes(buffer[:self.frame_bytes])
                del buffer[:self.frame_bytes]
                now = self._clock()
                if now - started < SETTLE_SECONDS:
                    continue
                self._sample(frame, now)
            self._housekeeping()

    def _sample(self, frame: bytes, now: float) -> None:
        self._samples += 1
        self._frames.append(frame)
        observation = self.detector.observe(grid_from_frame(frame, self.frame_size), now)
        self._state = "warming" if observation.verdict == "warming" else "armed"
        if observation.triggered:
            self._would_trigger(observation)

    def _would_trigger(self, observation: Observation) -> None:
        self._would_triggers += 1
        at = self._wall()
        action = self._act(observation)
        features = observation.features
        LOGGER.info(
            "gate_early_trigger stage=would_trigger source=vision mode=%s light=%s action=%s "
            "blob=%.3f changed=%.3f scatter=%.3f luma=%.0f luma_jump=%+.0f peak=%d "
            "persistence=%d cx=%s cy=%s track_dx=%s growth=%s scene_difference=%.3f",
            self.config.mode, observation.light, action,
            features["blob_fraction"], features["changed_fraction"], features["scatter"],
            features["mean_luma"], features["luma_jump"], features["peak"],
            features["persistence"], features["cx"], features["cy"],
            features.get("track_dx"), features.get("growth"), features["scene_difference"],
        )
        row = None
        if self._store is not None:
            row = self._store.record(
                kind=KIND_WOULD_TRIGGER, source=SOURCE_VISION, at_epoch=at,
                mode=self.config.mode, light=observation.light, detector_state=self._state,
                features=features, action=action, thumbnail=self._thumbnail(at, "trigger"),
            )
        if action == "scheduled":
            self._pending_sweep_row = row
        if self._layers is not None:
            store = self._store
            self._layers.begin(
                lambda layers, _row=row: store.annotate(_row, layers=layers)
                if store is not None else None
            )

    def _act(self, observation: Observation) -> str:
        """Ask for a local-only sweep. In ``shadow`` this is where it stops."""
        if self.config.mode != MODE_ON:
            return "none"
        start = getattr(self._capture, "on_early_trigger", None)
        if not callable(start):
            return "unavailable"
        now = self._clock()
        if not self._hours.open_at(datetime.fromtimestamp(self._wall(), timezone.utc)):
            return "skipped_hours"
        with self._lock:
            if self._backoff_until is not None and now < self._backoff_until:
                return "skipped_backoff"
            if (
                self._last_acted_at is not None
                and now - self._last_acted_at < self.config.min_interval_seconds
            ):
                return "skipped_interval"
            while self._unconfirmed and now - self._unconfirmed[0] >= 3600.0:
                self._unconfirmed.popleft()
            if len(self._unconfirmed) >= self.config.max_per_hour:
                self._backoff_until = now + self._backoff_seconds
                LOGGER.warning(
                    "gate_early_trigger stage=backoff unconfirmed_last_hour=%d seconds=%d",
                    len(self._unconfirmed), round(self._backoff_seconds),
                )
                self._backoff_seconds = min(MAX_BACKOFF_SECONDS, self._backoff_seconds * 2.0)
                self._unconfirmed.clear()
                return "skipped_backoff"
        try:
            outcome = str(start(dict(observation.features)))
        except Exception:
            LOGGER.warning("gate_early_trigger stage=start_failed", exc_info=True)
            return "error"
        if outcome == "scheduled":
            self._scheduled += 1
            with self._lock:
                self._last_acted_at = now
                # Unconfirmed until a camera alarm says otherwise.
                self._unconfirmed.append(now)
        return outcome

    def _thumbnail(self, at_epoch: float, label: str) -> str | None:
        if self._thumbnails is None or not self.config.thumbnails or not self._frames:
            return None
        frames = list(self._frames)
        before = frames[0] if len(frames) > 1 else None
        return self._thumbnails.save(before, frames[-1], self.frame_size, at_epoch, label)

    def _housekeeping(self) -> None:
        now = self._clock()
        if self._store is not None and now - self._last_correlated_at >= CORRELATE_EVERY_SECONDS:
            self._last_correlated_at = now
            self._store.correlate(self._wall())
        if now - self._last_status_at >= STATUS_EVERY_SECONDS:
            self._last_status_at = now
            LOGGER.info("gate_early_trigger stage=status %s", json.dumps(self.status(), sort_keys=True))

    def status(self) -> dict:
        last = self.detector.last
        return {
            "mode": self.config.mode, "state": self._state, "light": self.detector.light,
            "samples": self._samples, "would_triggers": self._would_triggers,
            "scheduled": self._scheduled, "camera_alarms": self._camera_alarms,
            "restarts": self._restarts, "pauses": self._pauses,
            "backoff": self._backoff_until is not None and self._clock() < self._backoff_until,
            "last_verdict": None if last is None else last.verdict,
        }

    def close(self) -> None:
        self._closed = True
        self._stop_child()

    def _stop_child(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        except Exception:
            pass


def _read_some(stream, size: int, *, timeout: float):
    """Bytes from the child, ``b""`` at its end, or None when nothing came in time.

    A real pipe is waited on with ``select`` so a stalled stream cannot hold
    the loop past a pause or a shutdown; anything else (a test's in-memory
    stream) is simply read.
    """
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return stream.read(size)
    import select

    ready, _, _ = select.select([descriptor], [], [], timeout)
    if not ready:
        return None
    return os.read(descriptor, max(size, 65536))


def build_worker(environment=None, *, state_directory: Path, events_database: Path | None = None,
                 capture=None, activity=None, clear_stream=None, sweep_reader=None,
                 farm_machinery=None, decoder_arguments: tuple = (),
                 download_filters: tuple = ()) -> EarlyTriggerWorker | None:
    """The worker the controller runs, or None when the feature is ``off``.

    ``off`` builds nothing: no thread, no child, no file.
    """
    config = load_config(environment, state_directory)
    if not config.enabled:
        return None
    state_directory = Path(state_directory)
    layers = ConfirmationLayers(
        clip_look=getattr(farm_machinery, "look", None),  # or `set_clip_look`, later
        lane_frame=_lane_frame_source(
            clear_stream, config.patch, tuple(decoder_arguments), tuple(download_filters)),
        plate_frame=_plate_frame_source(clear_stream),
        plate_read=getattr(sweep_reader, "read", None),
        busy=activity.busy_reason if activity is not None else None,
        per_minute=config.layers_per_minute, sleep=sleep,
    )
    return EarlyTriggerWorker(
        config, capture=capture, activity=activity,
        store=EarlyTriggerStore(state_directory / "early-trigger.db", events_database),
        layers=layers,
        thumbnails=ThumbnailStore(
            state_directory / "early-trigger-thumbnails",
            max_files=config.thumbnail_max, max_days=config.thumbnail_days,
        ),
    )


def lane_square(patch: PlateRegion) -> tuple[float, float, float, float]:
    """A square (in 16:9 pixels) of the frame around the patch, as x,y,w,h fractions.

    The image tower looks at the centre square of what it is given. Handing it
    the whole frame would show it the gate, not the lane; this is the smallest
    square that holds the whole patch, slid back inside the frame.
    """
    side_px = max(patch.width * 16.0, patch.height * 9.0)
    width, height = min(1.0, side_px / 16.0), min(1.0, side_px / 9.0)
    x = min(max(0.0, patch.x + patch.width / 2.0 - width / 2.0), 1.0 - width)
    y = min(max(0.0, patch.y + patch.height / 2.0 - height / 2.0), 1.0 - height)
    return round(x, 4), round(y, 4), round(width, 4), round(height, 4)


def _lane_frame_source(clear_stream, patch: PlateRegion, decoder_arguments: tuple = (),
                       download_filters: tuple = ()):
    """The newest clear-stream keyframe, cut to the lane square at 448 px. Or None."""
    ring = getattr(clear_stream, "ring", None)
    if ring is None or not callable(getattr(ring, "latest_keyframe", None)):
        return None
    x, y, width, height = lane_square(patch)
    crop = (
        f"crop=trunc(iw*{width:.4f}/2)*2:trunc(ih*{height:.4f}/2)*2"
        f":trunc(iw*{x:.4f}/2)*2:trunc(ih*{y:.4f}/2)*2"
    )

    def frame():
        from .clear_stream import decode_command, decode_frames

        latest = ring.latest_keyframe()
        if latest is None:
            return None
        command = decode_command(
            decoder_arguments=decoder_arguments,
            filters=download_filters + (crop, "scale=448:448"), frames=1,
        )
        frames = decode_frames(latest[0], command, timeout=3.0)
        return frames[-1] if frames else None

    return frame


def _plate_frame_source(clear_stream):
    decode = getattr(clear_stream, "decode_latest_keyframe", None)
    if not callable(decode):
        return None

    def frame():
        latest = decode()
        return None if latest is None else latest[0]

    return frame
