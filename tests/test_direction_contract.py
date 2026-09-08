"""The app's ``telemetry.direction`` contract, transcribed and enforced here.

The authority is ``validateDirection`` in access-gate-ui
``worker/contracts/gate-event-ingest/contract.ts`` (deployed as #49). The
Worker allowlists keys strictly and **rejects the whole event** for anything
it does not recognise; a rejected event is a 400 the Pi's outbox retries
forever. So the rules are transcribed here as executable Python, and every
block the controller can produce is run through them.

Transcribed, line for line:

    const DIRECTION_KEYS = ['verdict', 'method', 'score', 'slope', 'frames', 'span_ms'];
    const DIRECTION_VERDICT = /^(?:entering|exiting|stationary|unknown)$/;
    const DIRECTION_METHOD = /^(?:box_width|none)$/;
    const MAX_DIRECTION_SLOPE = 10;
    const MAX_DIRECTION_FRAMES = 64;
    const MAX_DIRECTION_SPAN_MS = 600_000;
    const FORBIDDEN_TELEMETRY_KEY = /token|secret|password|credential|path|raw_response|exception/i;

    score  = optionalNumber(direction.score, 0, 1, ...)
    slope  = optionalNumber(direction.slope, -MAX_DIRECTION_SLOPE, MAX_DIRECTION_SLOPE, ...)
    frames = requireInteger(direction.frames, 0, MAX_DIRECTION_FRAMES, ...)
    spanMs = requireInteger(direction.span_ms, 0, MAX_DIRECTION_SPAN_MS, ...)
    if (method === 'none' && verdict !== 'unknown') throw ...

Update this file and the estimator together, never one alone.
"""

import json
import math
import re
import unittest

from gate_controller.direction import (
    DirectionConfig,
    DirectionEstimate,
    MAX_SAMPLES,
    METHOD_BOX_WIDTH,
    METHOD_NONE,
    SOURCE_VEHICLE_BOX,
    VERDICTS,
    estimate_direction,
)
from gate_controller.telemetry import (
    DirectionTelemetry, EventTelemetry, StageDurations,
)


DIRECTION_KEYS = ("verdict", "method", "score", "slope", "frames", "span_ms")
DIRECTION_VERDICT = re.compile(r"^(?:entering|exiting|stationary|unknown)$")
DIRECTION_METHOD = re.compile(r"^(?:box_width|none)$")
MAX_DIRECTION_SLOPE = 10
MAX_DIRECTION_FRAMES = 64
MAX_DIRECTION_SPAN_MS = 600_000
FORBIDDEN_TELEMETRY_KEY = re.compile(
    r"token|secret|password|credential|path|raw_response|exception", re.IGNORECASE
)
#: `direction` has to be on the telemetry allowlist for the block to be legal
#: at all; it is, at schema_version 3, so no version bump belongs to this
#: change.
TELEMETRY_KEYS = (
    "trace_id", "taxonomy_version", "trigger", "stage_timestamps",
    "stage_durations", "frames", "ocr_attempts", "decision", "actuation",
    "delivery", "match_policy", "local_ocr", "direction",
)


class ContractRejection(Exception):
    """What a 400 from the Worker's ingest looks like from here."""


def require_integer(value, minimum, maximum, field):
    # Number.isSafeInteger: not a boolean, an exact integer, in range.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractRejection(f"{field} is invalid")
    if not minimum <= value <= maximum:
        raise ContractRejection(f"{field} is invalid")
    return value


def require_number(value, minimum, maximum, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractRejection(f"{field} is invalid")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ContractRejection(f"{field} is invalid")
    return value


def optional_number(value, minimum, maximum, field):
    if value is None:
        return None
    return require_number(value, minimum, maximum, field)


def require_pattern(value, pattern, field):
    if not isinstance(value, str) or not pattern.match(value):
        raise ContractRejection(f"{field} is invalid")
    return value


def assert_allowed_keys(record, allowed, field):
    for key in record:
        if FORBIDDEN_TELEMETRY_KEY.search(key) or key not in allowed:
            raise ContractRejection(f"{field}.{key} is not allowed")


def validate_direction(block):
    """``validateDirection``, transcribed."""
    if not isinstance(block, dict):
        raise ContractRejection("telemetry.direction must be an object")
    assert_allowed_keys(block, DIRECTION_KEYS, "telemetry.direction")
    verdict = require_pattern(
        block.get("verdict"), DIRECTION_VERDICT, "telemetry.direction.verdict"
    )
    method = require_pattern(
        block.get("method"), DIRECTION_METHOD, "telemetry.direction.method"
    )
    score = optional_number(block.get("score"), 0, 1, "telemetry.direction.score")
    slope = optional_number(
        block.get("slope"), -MAX_DIRECTION_SLOPE, MAX_DIRECTION_SLOPE,
        "telemetry.direction.slope",
    )
    frames = require_integer(
        block.get("frames"), 0, MAX_DIRECTION_FRAMES, "telemetry.direction.frames"
    )
    span_ms = require_integer(
        block.get("span_ms"), 0, MAX_DIRECTION_SPAN_MS, "telemetry.direction.span_ms"
    )
    if method == "none" and verdict != "unknown":
        raise ContractRejection("telemetry.direction verdict is unsupported by its method")
    return {
        "verdict": verdict, "method": method, "score": score, "slope": slope,
        "frames": frames, "spanMs": span_ms,
    }


class TranscriptionTests(unittest.TestCase):
    def test_the_estimator_and_the_contract_share_one_vocabulary(self):
        self.assertEqual(
            {verdict for verdict in VERDICTS},
            {"entering", "exiting", "stationary", "unknown"},
        )
        for verdict in VERDICTS:
            self.assertRegex(verdict, DIRECTION_VERDICT)
        for method in (METHOD_BOX_WIDTH, METHOD_NONE):
            self.assertRegex(method, DIRECTION_METHOD)

    def test_the_block_carries_exactly_the_contract_keys(self):
        block = DirectionEstimate().to_wire()

        self.assertEqual(tuple(block), DIRECTION_KEYS)

    def test_no_key_of_this_block_may_ever_mention_a_plate(self):
        """The direction block is geometry, never characters off a plate."""
        for key in DIRECTION_KEYS:
            self.assertNotIn("plate", key)
        with self.assertRaises(ContractRejection):
            validate_direction({
                **DirectionEstimate().to_wire(), "plate": "12D3456",
            })

    def test_direction_is_allowlisted_at_the_top_of_the_telemetry_block(self):
        self.assertIn("direction", TELEMETRY_KEYS)


class EveryProducibleBlockTests(unittest.TestCase):
    """Whatever the estimator says, ingest must accept it."""

    def blocks(self):
        config = DirectionConfig()
        series = (
            (),
            ((0.0, 0.4),),
            ((0.0, 0.4), (1.0, 0.2)),
            ((0.0, 0.4), (1.5, 0.2), (3.0, 0.1)),
            ((0.0, 0.1), (1.5, 0.2), (3.0, 0.4)),
            ((0.0, 0.3), (2.0, 0.3), (4.0, 0.3), (6.0, 0.3)),
            ((0.0, 0.3), (10.0, 0.24), (20.0, 0.19), (30.0, 0.15)),
            tuple((index * 0.5, 0.4 * math.exp(-2.0 * index * 0.5))
                  for index in range(40)),
            tuple((index * 100.0, 0.01 + index * 0.01) for index in range(8)),
        )
        for samples in series:
            yield estimate_direction(samples, config, source=SOURCE_VEHICLE_BOX)

    def test_every_estimate_the_controller_can_make_is_accepted(self):
        for estimate in self.blocks():
            with self.subTest(estimate=estimate):
                validate_direction(estimate.to_wire())

    def test_every_estimate_survives_the_json_round_trip(self):
        for estimate in self.blocks():
            with self.subTest(estimate=estimate):
                encoded = json.dumps(estimate.to_wire())
                # `Infinity` and `NaN` are what a lax encoder emits and no
                # strict reader can parse; neither may ever appear.
                self.assertNotIn("Infinity", encoded)
                self.assertNotIn("NaN", encoded)
                validate_direction(json.loads(encoded))

    def test_a_slope_beyond_the_bound_is_clamped_rather_than_refused(self):
        """A two-frame burst on ingress timestamps can produce +2.8."""
        block = DirectionEstimate(
            verdict="exiting", method=METHOD_BOX_WIDTH, slope=-1e9,
            score=0.9, frames=4, span_ms=3_000,
        ).to_wire()

        self.assertEqual(validate_direction(block)["slope"], -MAX_DIRECTION_SLOPE)

    def test_an_out_of_range_score_frames_or_span_is_clamped(self):
        block = DirectionEstimate(
            verdict="entering", method=METHOD_BOX_WIDTH, score=17.0,
            slope=0.05, frames=10_000, span_ms=10 ** 9,
        ).to_wire()

        parsed = validate_direction(block)

        self.assertEqual(parsed["score"], 1.0)
        # The controller clamps to its own sample cap, which is stricter than
        # the contract's ceiling; the contract is what it may never exceed.
        self.assertEqual(parsed["frames"], MAX_SAMPLES)
        self.assertLessEqual(parsed["frames"], MAX_DIRECTION_FRAMES)
        self.assertEqual(parsed["spanMs"], MAX_DIRECTION_SPAN_MS)

    def test_a_verdict_unsupported_by_its_method_is_repaired_before_it_ships(self):
        """`method: none` may only ever carry `unknown`."""
        block = DirectionEstimate(verdict="exiting", method=METHOD_NONE).to_wire()

        self.assertEqual(block["verdict"], "unknown")
        validate_direction(block)

    def test_a_value_outside_the_vocabulary_falls_back_rather_than_shipping(self):
        block = DirectionEstimate(verdict="reversing", method="lidar").to_wire()

        self.assertEqual(block["verdict"], "unknown")
        self.assertEqual(block["method"], "none")
        validate_direction(block)

    def test_the_telemetry_layer_narrows_the_block_a_second_time(self):
        wire = EventTelemetry(
            trace_id="8f14e45f-ceea-467a-9fdd-2a4a2b1a3f01",
            stage_durations=StageDurations(),
            frames=(), ocr_attempts=(),
            decision_outcome="denied", decision_reason="no_match",
            actuation_claim="not_claimed", actuation_attempted=False,
            relay_outcome="not_attempted", outbox_attempt=0,
            delivery_state="pending",
            direction=DirectionTelemetry.from_block({
                "verdict": "exiting", "method": "box_width", "score": 5.0,
                "slope": float("inf"), "frames": 4.6, "span_ms": -12,
            }),
        ).to_wire()

        parsed = validate_direction(wire["direction"])

        self.assertEqual(parsed["score"], 1.0)
        self.assertIsNone(parsed["slope"])
        self.assertEqual(parsed["frames"], 5)
        self.assertEqual(parsed["spanMs"], 0)

    def test_a_block_from_a_dict_of_rubbish_is_still_shippable(self):
        wire = DirectionTelemetry.from_block({
            "verdict": None, "method": ["box_width"], "score": "high",
            "slope": "steep", "frames": None, "span_ms": "long",
        }).to_wire()

        parsed = validate_direction(wire)

        self.assertEqual(parsed["verdict"], "unknown")
        self.assertEqual(parsed["method"], "none")


class TheValidatorItselfTests(unittest.TestCase):
    """The transcription has to refuse what the Worker refuses."""

    def valid(self, **overrides):
        block = {
            "verdict": "exiting", "method": "box_width", "score": 0.5,
            "slope": -0.4, "frames": 4, "span_ms": 3_000,
        }
        block.update(overrides)
        return block

    def test_the_baseline_block_is_accepted(self):
        validate_direction(self.valid())

    def test_refusals(self):
        refused = (
            {"verdict": "reversing"},
            {"method": "lidar"},
            {"score": 1.5},
            {"score": float("nan")},
            {"slope": 11},
            {"slope": -11},
            {"frames": 65},
            {"frames": 3.5},
            {"frames": True},
            {"frames": None},
            {"span_ms": 600_001},
            {"span_ms": -1},
            {"span_ms": None},
            {"method": "none"},
        )
        for overrides in refused:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ContractRejection):
                    validate_direction(self.valid(**overrides))

    def test_an_unknown_or_forbidden_key_rejects_the_whole_block(self):
        for extra in ("source", "image_path", "api_token", "raw_response"):
            with self.subTest(extra=extra):
                with self.assertRaises(ContractRejection):
                    validate_direction(self.valid(**{extra: "x"}))

    def test_score_and_slope_are_nullable_and_the_others_are_not(self):
        validate_direction(self.valid(score=None, slope=None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
