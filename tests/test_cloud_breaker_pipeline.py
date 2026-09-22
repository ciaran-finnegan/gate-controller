"""2026-09-22 15:18 IST: the link was lossy, not down, and the cloud was still asked.

#183 stops handing frames to the cloud plate reader while the network probe's
last TLS open says ``internet=failed``. But the farm's link is mostly *lossy*:
at 40 % packet loss the probe's single TLS open succeeds (``internet=ok``), so
every passage still handed up to five frames to the cloud reader and each one
died after ~6 s -- ``gate_ocr stage=attempt_failed cause=connection_error
detail=TimeoutError``, ``stage=retry cause=connection_error wait_ms=1050``, a
``decision_timeout`` denial row and a ``queue_coalesced`` skip for a passage
the gate had *already opened* on a local read; 220 ConnectionError and 19
ReadTimeout in the outbox alone over the previous three hours. The cost was
the sweep's read budget, 6 s stalls in the cloud lane, denial rows uploaded
over the same bad link, and lookups billed for timeouts.

So the cloud client now keeps a circuit breaker fed by its own outcomes
(``ocr.CloudBreaker``): three requests that die on the link without an answer
open it, an open breaker removes the request (the same fast skip #183 added,
``reason=cloud_unreachable``), it re-opens for a single trial after a bounded
time, and any answer from the cloud closes it. Everything here goes the way
the alarm goes, on the harness ``tests/test_sweep_pipeline.py`` built:

    on_camera_event -> run_forever -> local_sweep -> the worker's injector ->
    the burst queue -> GateProcessor.prepare -> GateProcessor.process ->
    ActuationCoordinator -> the relay

with the breaker wired exactly as ``__main__`` wires it: one ``CloudBreaker``
handed to the cloud client, read by the sweep and the processor through one
``CloudAvailability`` (the probe's answer AND the breaker's), and closed by the
real ``NetProbeWorker`` when it sees the link come back. The HTTP session is
the spy and the trap at once: a request that leaves is written down *and*
blocks for as long as the real one did, so a leak fails on the count and on
the clock.

The rule fails open towards the cloud and never towards the gate: an open
breaker can only remove a network call that would have died the same way. A
locally authorised plate opens exactly as it always has, and a breaker that
cannot be read is a closed one.
"""
import ast
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from time import monotonic, sleep
from unittest import mock

import requests
from PIL import Image

from gate_controller.local_sweep import crop_to_region
from gate_controller.net_probe import NetProbeConfig, NetProbeWorker
from gate_controller.ocr import (
    BREAKER_FAILURE_THRESHOLD, BREAKER_OPEN_MAX_SECONDS, BREAKER_OPEN_SECONDS,
    CloudAvailability, CloudBreaker, PlateRecognizerClient,
)
from gate_controller.processor import GateProcessor
from gate_controller.trigger_capture import TriggerFrameCapture
from tests import test_net_probe as probe_fakes
from tests import test_sweep_pipeline as harness
from tests.test_internet_down_pipeline import DEAD_LINK_STALL_SECONDS, Link, unreadable_passage
from tests.test_local_recognizer import FakeResponse, FakeSession, cloud_payload, read, recognizer
from tests.test_sweep_pipeline import REGION, CapturedLogs, Gate, digest, frame, wait_for

PACKAGE = Path(__file__).resolve().parents[1] / "gate_controller"


class ShiftedClock:
    """The breaker's monotonic clock, which a test can move forward.

    Only the breaker runs on it: the client's pacing, the processor's
    deadlines and the sweep keep real time, so nothing else in the pipeline
    is fooled -- what moves is only how long the breaker believes it has been
    open.
    """

    def __init__(self):
        self.offset = 0.0

    def __call__(self) -> float:
        return monotonic() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


class LossyCloud(FakeSession):
    """The cloud reader over the 2026-09-22 link, switched per passage.

    ``DROP``: the request dies on the link at once (a ``ReadTimeout``, which
    the client never retries -- the 15:18 shape, minus the wait). ``STALL``:
    the real thing, ~6 s and then the same ``ReadTimeout``. ``ANSWER``: a
    healthy link, answering at once and finding nothing. ``HTTP_500``: the
    link is fine and the service is not.
    """

    DROP = "drop"
    STALL = "stall"
    ANSWER = "answer"
    HTTP_500 = "http_500"

    def __init__(self, mode: str, *, drop_stall: float = 0.05,
                 stall: float = DEAD_LINK_STALL_SECONDS):
        super().__init__([])
        self.mode = mode
        self.drop_stall = drop_stall
        self.stall = stall
        self.posted_at = []

    def post(self, *args, **kwargs):
        self.posted_at.append(monotonic())
        if self.mode == self.DROP:
            sleep(self.drop_stall)
            raise requests.exceptions.ReadTimeout("simulated lossy link")
        if self.mode == self.STALL:
            sleep(self.stall)
            raise requests.exceptions.ReadTimeout("simulated dead link")
        if self.mode == self.HTTP_500:
            return FakeResponse({"detail": "service unavailable"}, status_code=500)
        return FakeResponse({"results": []})


class HookedLink(Link):
    """The probe as ``main`` builds it: told what to call when the link returns."""

    def __init__(self, *, up: bool, on_internet_restored):
        super().__init__(up=up)
        self.probe = NetProbeWorker(
            NetProbeConfig(enabled=True),
            popen=lambda command, **kwargs: probe_fakes.FakePopen(
                probe_fakes.HEALTHY_PING.encode()
            )(command, **kwargs),
            clock=lambda: self.now,
            host_metrics=lambda **_: dict(probe_fakes.HEALTHY_METRICS),
            proc_root=Path("/nonexistent-proc"),
            sys_class_net=Path("/nonexistent-sys"),
            internet_connect=self._open,
            on_internet_restored=on_internet_restored,
        )


def breaker_gate(test, breaker, link=None, **options) -> Gate:
    """The harness's gate, wired to the breaker the way ``__main__`` wires it.

    ``main`` builds one ``CloudBreaker``, hands it to the cloud client, and
    hands the sweep and the processor one ``CloudAvailability`` over it and
    the probe's own ``internet_reachable``; the processor passes that on to
    the client. ``None`` is a controller with the probe switched off.
    """
    predicate = None if link is None else link.probe.internet_reachable
    available = CloudAvailability(breaker, predicate)
    with mock.patch.object(
        harness, "TriggerFrameCapture",
        partial(TriggerFrameCapture, internet_reachable=available),
    ), mock.patch.object(
        harness, "GateProcessor", partial(GateProcessor, internet_reachable=available),
    ), mock.patch.object(
        harness, "PlateRecognizerClient",
        partial(PlateRecognizerClient, cloud_breaker=breaker),
    ):
        return Gate(test, **options)


def open_for_pattern() -> str:
    """``for_s`` after the test moved the breaker's clock past the open time.

    The breaker's clock is real time plus the offset, so the passage that
    followed adds its own second or two: 61 s is written as ``6\\d``.
    """
    return rf"{int(BREAKER_OPEN_SECONDS) // 10}\d"


def authorised_passage(seeds):
    """Frames the device reads as the authorised plate at 0.95: over every bar."""
    frames = [frame(seed) for seed in seeds]
    answers = {digest(crop_to_region(data, REGION)): ("10CE1990", 0.95) for data in frames}
    return frames, answers


class CloudBreakerPassageTests(unittest.TestCase):
    """Whole passages, through the real pipeline, with the breaker wired as shipped."""

    SWEEP_SECONDS = 1.5

    def setUp(self):
        self.gate = None
        self.clock = ShiftedClock()
        self.breaker = CloudBreaker(clock=self.clock)

    def tearDown(self):
        if self.gate is not None:
            self.gate.close()

    def _gate(self, link=None, *, breaker=None, **options):
        self.gate = breaker_gate(
            self, breaker if breaker is not None else self.breaker, link,
            sweep_seconds=self.SWEEP_SECONDS, cloud_frames=5, fallback=1, **options,
        )
        return self.gate

    def _run_passage(self, gate, frames, *, logs):
        """One alarm, its sweep to the end, at least one new result, and the alarm time."""
        already = len(gate.outcomes())
        ended = logs.text().count("gate_local_sweep outcome=ended")
        gate.frames.script([(0.05 + index * 0.12, data) for index, data in enumerate(frames)])
        alarm_at = monotonic()
        gate.alarm()
        self.assertTrue(
            wait_for(lambda: logs.text().count("gate_local_sweep outcome=ended") > ended, 12.0),
            f"the sweep never ended:\n{logs.text()}",
        )
        self.assertTrue(
            wait_for(lambda: len(gate.outcomes()) > already, 10.0),
            f"the passage was never decided: {gate.outcomes()}\n{logs.text()}",
        )
        # Give anything else that was going to happen the chance to.
        sleep(0.4)
        return alarm_at

    def _finished_at(self, gate):
        with gate._lock:
            return max(at for at, _paths, _result in gate.results)

    def _open_through_a_passage(self, gate, cloud, logs, seeds=range(500, 512)):
        """The 15:18 passage: the probe says ``ok``, the cloud reader dies three times.

        Returns the number of requests that left before the breaker opened.
        """
        frames, answers = unreadable_passage(seeds)
        gate.engine.answers.update(answers)
        cloud.mode = LossyCloud.DROP
        self._run_passage(gate, frames, logs=logs)
        self.assertEqual(
            len(cloud.posted_at), BREAKER_FAILURE_THRESHOLD,
            "the third death on the link opens the breaker; nothing leaves after it",
        )
        self.assertEqual(self.breaker.state, "open")
        text = logs.text()
        self.assertEqual(
            text.count(f"gate_ocr stage=cloud_breaker state=open after={BREAKER_FAILURE_THRESHOLD} "
                       f"failures for_s={BREAKER_OPEN_SECONDS:.0f}"), 1,
            f"the opening is journalled once:\n{text}",
        )
        return len(cloud.posted_at)

    # -- the rule ------------------------------------------------------------

    def test_three_deaths_on_the_link_open_the_breaker_and_the_next_passage_asks_nothing(self):
        """15:18 replayed. The probe says ``ok`` throughout -- its one TLS open
        got through -- so #183 lets every request go. Three die on the link;
        the breaker opens; the next passage hands nothing over, queues nothing
        for the cloud lane, posts nothing, and ends on the device's own answer
        inside the sweep window instead of after a 6 s stall per frame."""
        link = Link(up=True)
        self.assertEqual(link.measure(), "ok")
        cloud = LossyCloud(LossyCloud.DROP)
        gate = self._gate(link, answers={}, cloud=cloud)

        with CapturedLogs() as logs:
            posted = self._open_through_a_passage(gate, cloud, logs)
            handovers = gate.sweep_status()["cloud_handovers"]
            self.assertGreaterEqual(handovers, BREAKER_FAILURE_THRESHOLD)

            # The next passage: the link is what it was, and every request
            # would now block the full six seconds.
            cloud.mode = LossyCloud.STALL
            frames, answers = unreadable_passage(range(520, 532))
            gate.engine.answers.update(answers)
            before = len(logs.lines)
            alarm_at = self._run_passage(gate, frames, logs=logs)

        self.assertEqual(len(cloud.posted_at), posted, "a request left with the breaker open")
        self.assertEqual(gate.outcomes()[-1], (False, "no_match"),
                         "one clear denial for the passage, on the device's own answer")
        self.assertLess(
            self._finished_at(gate) - alarm_at, self.SWEEP_SECONDS + 2.0,
            "the passage stalled on a request that could not be answered",
        )
        self.assertEqual(gate.relay_calls, [])
        self.assertEqual(gate.sweep_status()["cloud_handovers"], handovers,
                         "the sweep handed a frame to a cloud it cannot reach")
        later = "\n".join(logs.lines[before:])
        self.assertEqual(
            later.count("gate_local_sweep stage=cloud_handover_skipped reason=cloud_unreachable of=5"), 1,
            f"journalled once per sweep, naming the breaker and not the link:\n{later}",
        )
        self.assertIn("gate_ocr stage=cloud_skipped reason=cloud_unreachable", later)
        self.assertNotIn("stage=cloud_handover frame=", later)
        self.assertNotIn("decision_timeout", later)
        self.assertNotIn("ocr_busy", later)
        self.assertNotIn("queue_coalesced", later)
        self.assertNotIn("internet_down", logs.text(), "the probe never said the link was down")
        self.assertEqual(logs.text().count("stage=cloud_breaker state="), 1,
                         "no transition is journalled twice")

    def test_half_open_lets_exactly_one_request_through_and_a_death_reopens_for_twice_as_long(self):
        link = Link(up=True)
        link.measure()
        cloud = LossyCloud(LossyCloud.DROP)
        gate = self._gate(link, answers={}, cloud=cloud)

        with CapturedLogs() as logs:
            posted = self._open_through_a_passage(gate, cloud, logs)
            self.clock.advance(BREAKER_OPEN_SECONDS + 1.0)
            self.assertTrue(self.breaker.available(), "the open time has run out")

            frames, answers = unreadable_passage(range(540, 552))
            gate.engine.answers.update(answers)
            before = len(logs.lines)
            self._run_passage(gate, frames, logs=logs)

        self.assertEqual(len(cloud.posted_at), posted + 1,
                         "exactly one trial request leaves a half-open breaker")
        later = "\n".join(logs.lines[before:])
        self.assertRegex(
            later,
            rf"gate_ocr stage=cloud_breaker state=half_open after={BREAKER_FAILURE_THRESHOLD} "
            rf"failures for_s={open_for_pattern()}\n",
        )
        self.assertIn(
            f"gate_ocr stage=cloud_breaker state=open after={BREAKER_FAILURE_THRESHOLD + 1} "
            f"failures for_s={BREAKER_OPEN_SECONDS * 2:.0f}", later,
            "the trial died: open again, for twice as long",
        )
        self.assertEqual(self.breaker.state, "open")
        self.assertIn("gate_ocr stage=cloud_skipped reason=cloud_unreachable", later,
                      "the frames behind the trial were not sent")
        self.assertEqual(gate.relay_calls, [])

    def test_a_trial_that_is_answered_closes_the_breaker_and_the_cloud_is_back(self):
        link = Link(up=True)
        link.measure()
        cloud = LossyCloud(LossyCloud.DROP)
        gate = self._gate(link, answers={}, cloud=cloud)

        with CapturedLogs() as logs:
            posted = self._open_through_a_passage(gate, cloud, logs)
            self.clock.advance(BREAKER_OPEN_SECONDS + 1.0)

            cloud.mode = LossyCloud.ANSWER
            frames, answers = unreadable_passage(range(560, 572))
            gate.engine.answers.update(answers)
            before = len(logs.lines)
            self._run_passage(gate, frames, logs=logs)

        self.assertGreaterEqual(len(cloud.posted_at), posted + 2,
                                "the trial was answered and the rest of the passage followed it")
        later = "\n".join(logs.lines[before:])
        self.assertRegex(
            later,
            rf"gate_ocr stage=cloud_breaker state=closed after={BREAKER_FAILURE_THRESHOLD} "
            rf"failures for_s={open_for_pattern()} reason=response",
        )
        self.assertEqual(self.breaker.state, "closed")
        self.assertEqual(self.breaker.status()["failures"], 0, "the count starts again")
        self.assertIn("stage=cloud_handover frame=", later)
        self.assertEqual(gate.outcomes()[-1], (False, "no_match"))
        self.assertEqual(gate.relay_calls, [])

    def test_an_http_500_is_an_answer_and_closes_the_breaker(self):
        """A 5xx says the link carried the request: the service is the problem,
        not the network, and the breaker is about the network only."""
        link = Link(up=True)
        link.measure()
        cloud = LossyCloud(LossyCloud.DROP)
        gate = self._gate(link, answers={}, cloud=cloud)

        with CapturedLogs() as logs:
            posted = self._open_through_a_passage(gate, cloud, logs)
            self.clock.advance(BREAKER_OPEN_SECONDS + 1.0)

            cloud.mode = LossyCloud.HTTP_500
            frames, answers = unreadable_passage(range(580, 592))
            gate.engine.answers.update(answers)
            before = len(logs.lines)
            self._run_passage(gate, frames, logs=logs)

        self.assertGreaterEqual(len(cloud.posted_at), posted + 2)
        later = "\n".join(logs.lines[before:])
        self.assertIn("gate_ocr stage=cloud_breaker state=closed", later)
        self.assertIn("reason=response", later)
        self.assertEqual(self.breaker.state, "closed")
        self.assertNotIn("cloud_unreachable", later)
        self.assertEqual(gate.relay_calls, [])

    # -- never towards the gate ------------------------------------------------

    def test_a_locally_authorised_plate_still_opens_once_with_the_breaker_open(self):
        link = Link(up=True)
        link.measure()
        cloud = LossyCloud(LossyCloud.DROP)
        gate = self._gate(link, answers={}, cloud=cloud)

        with CapturedLogs() as logs:
            posted = self._open_through_a_passage(gate, cloud, logs)
            cloud.mode = LossyCloud.STALL
            frames, answers = authorised_passage(range(600, 608))
            gate.engine.answers.update(answers)
            gate.frames.script([(0.05 + index * 0.12, data) for index, data in enumerate(frames)])
            gate.alarm()
            self.assertTrue(wait_for(gate.opened), f"never opened: {gate.outcomes()}\n{logs.text()}")
            self.assertTrue(wait_for(
                lambda: logs.text().count("gate_local_sweep outcome=ended") >= 2,
            ))

        self.assertEqual(gate.relay_calls, ["relay"], "exactly one pulse")
        self.assertEqual(len(cloud.posted_at), posted, "nothing was asked of the cloud")
        self.assertEqual(gate.opened_result().reason, "exact_match")
        self.assertEqual(gate.stored(gate.opened_result())["source"], "local")
        self.assertIn("gate_local_sweep outcome=ended reason=opened", logs.text())
        self.assertEqual(self.breaker.state, "open", "the local decision fed the breaker nothing")

    def test_a_breaker_that_cannot_be_read_leaves_the_cloud_exactly_as_before(self):
        class Unreadable(CloudBreaker):
            def available(self):
                raise RuntimeError("breaker state lost")

            def admit(self):
                raise RuntimeError("breaker state lost")

            def record_outcome(self, error=None):
                raise RuntimeError("breaker state lost")

            def abandon_trial(self):
                raise RuntimeError("breaker state lost")

            def status(self):
                raise RuntimeError("breaker state lost")

        link = Link(up=True)
        link.measure()
        cloud = LossyCloud(LossyCloud.DROP)
        gate = self._gate(link, breaker=Unreadable(), answers={}, cloud=cloud)
        frames, answers = unreadable_passage(range(620, 632))
        gate.engine.answers.update(answers)

        with CapturedLogs() as logs:
            self._run_passage(gate, frames, logs=logs)

        self.assertGreater(len(cloud.posted_at), BREAKER_FAILURE_THRESHOLD,
                           "an unreadable breaker held the cloud back")
        self.assertNotIn("cloud_unreachable", logs.text())
        self.assertNotIn("stage=cloud_breaker", logs.text())
        self.assertEqual(gate.relay_calls, [])

    # -- recovery ----------------------------------------------------------------

    def test_the_probe_seeing_the_link_return_closes_the_breaker_at_once(self):
        """The link went from lossy to down to back. The breaker opened on the
        lossy part and would have stayed open for its full timer; the probe's
        first ``ok`` after ``failed`` closes it, so the passage after the link
        returns goes to the cloud with no wait and no restart."""
        link = HookedLink(up=True, on_internet_restored=self.breaker.internet_restored)
        self.assertEqual(link.measure(), "ok")
        cloud = LossyCloud(LossyCloud.DROP)
        gate = self._gate(link, answers={}, cloud=cloud)

        with CapturedLogs() as logs:
            posted = self._open_through_a_passage(gate, cloud, logs)
            link.up = False
            # A healthy open is kept for the five-minute cadence; the next
            # cycle past it measures again and sees the link down.
            self.assertEqual(link.measure(after=301.0), "failed")
            self.assertEqual(self.breaker.state, "open", "going down changes nothing")
            link.up = True
            self.assertEqual(link.measure(after=60.0), "ok")
            self.assertEqual(self.breaker.state, "closed", "the link is back: closed at once")
            self.assertRegex(
                logs.text(),
                r"gate_ocr stage=cloud_breaker state=closed after=3 failures for_s=\d "
                r"reason=internet_restored",
            )
            self.assertFalse(self.clock.offset, "the breaker's own timer had not run out")

            cloud.mode = LossyCloud.ANSWER
            frames, answers = unreadable_passage(range(640, 652))
            gate.engine.answers.update(answers)
            before = len(logs.lines)
            self._run_passage(gate, frames, logs=logs)

        self.assertGreaterEqual(len(cloud.posted_at), posted + 1, "the cloud never came back")
        later = "\n".join(logs.lines[before:])
        self.assertIn("stage=cloud_handover frame=", later)
        self.assertNotIn("cloud_unreachable", later)
        self.assertNotIn("internet_down", later)

    def test_the_probes_definite_failed_is_still_honoured_and_named_as_such(self):
        """#183 unchanged: with the probe saying ``failed`` the request is
        removed for that reason, the breaker is not fed (nothing left), and the
        journal blames the link, not the breaker."""
        link = Link(up=False)
        self.assertEqual(link.measure(), "failed")
        cloud = LossyCloud(LossyCloud.STALL)
        gate = self._gate(link, answers={}, cloud=cloud)
        frames, answers = unreadable_passage(range(660, 672))
        gate.engine.answers.update(answers)

        with CapturedLogs() as logs:
            alarm_at = self._run_passage(gate, frames, logs=logs)

        self.assertEqual(cloud.posted_at, [])
        self.assertLess(self._finished_at(gate) - alarm_at, self.SWEEP_SECONDS + 2.0)
        self.assertEqual(self.breaker.state, "closed")
        self.assertEqual(self.breaker.status()["failures"], 0)
        text = logs.text()
        self.assertEqual(
            text.count("gate_local_sweep stage=cloud_handover_skipped reason=internet_down of=5"), 1,
        )
        self.assertIn("gate_ocr stage=cloud_skipped reason=internet_down", text)
        self.assertNotIn("cloud_unreachable", text)


class CloudBreakerClientTests(unittest.TestCase):
    """The request site alone: what feeds the breaker, and what it removes."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "frame.jpg"
        Image.new("L", (64, 32), color=128).save(self.path, format="JPEG")
        self.clock = ShiftedClock()
        self.breaker = CloudBreaker(clock=self.clock)

    def tearDown(self):
        self.directory.cleanup()

    def _client(self, session, local=None):
        client = PlateRecognizerClient(
            "token", session=session, local_recognizer=local,
            authorised=lambda: {"12D3456"}, cloud_breaker=self.breaker,
        )
        self.addCleanup(client.close)
        return client

    def _open(self, client, session):
        """Three ``ReadTimeout``s through the real request site.

        Without a local read the death propagates; in ``always`` mode the
        on-device read has already answered and the death costs a label, not
        the decision. Either way the breaker heard it.
        """
        session.error = requests.exceptions.ReadTimeout("lossy")
        posted = len(session.calls)
        for _ in range(BREAKER_FAILURE_THRESHOLD):
            try:
                client.recognise(self.path)
            except requests.exceptions.ReadTimeout:
                pass
        self.assertEqual(len(session.calls), posted + BREAKER_FAILURE_THRESHOLD)
        self.assertEqual(self.breaker.state, "open")
        session.error = None

    def test_the_client_owns_a_breaker_even_when_handed_none(self):
        client = PlateRecognizerClient("token", session=FakeSession([]))
        self.addCleanup(client.close)
        self.assertIsInstance(client.cloud_breaker, CloudBreaker)
        self.assertEqual(client.cloud_breaker.state, "closed")

    def test_an_open_breaker_removes_the_request_and_answers_no_plate(self):
        session = harness_session()
        client = self._client(session)
        self._open(client, session)
        posted = len(session.calls)

        with self.assertLogs("gate_controller.ocr", level="INFO") as logs:
            observation = client.recognise(self.path, trace_id="trace-open")

        self.assertEqual(len(session.calls), posted, "the request left with the breaker open")
        self.assertIsNone(observation.plate)
        self.assertEqual(observation.source, "local")
        self.assertFalse(observation.cloud_lookup, "nothing was billed")
        self.assertIn("gate_ocr stage=cloud_skipped reason=cloud_unreachable", "\n".join(logs.output))

    def test_in_always_mode_the_on_device_read_still_decides_and_the_label_is_not_sent(self):
        # One scripted read per frame: three to open the breaker, one to keep.
        local = recognizer(
            [read("12D3456", 0.99)] * (BREAKER_FAILURE_THRESHOLD + 1),
            mode="active", cloud="always",
        )
        self.addCleanup(local.close)
        session = harness_session()
        client = self._client(session, local)
        self._open(client, session)
        posted = len(session.calls)

        observation = client.recognise(self.path, trace_id="trace-always-open")

        self.assertEqual(observation.plate, "12D3456", "the local read was taken and kept")
        self.assertEqual(observation.source, "local")
        self.assertEqual(len(session.calls), posted, "the label request went out with the breaker open")

    def test_a_connection_error_and_its_retry_both_count(self):
        """The 15:18 shape: ``connection_error``, one retry on a fresh session
        1.05 s later, another. Two deaths per frame; the second frame's first
        death is the third, and its retry is the first request removed."""
        session = harness_session()
        session.error = requests.exceptions.ConnectionError("reset")
        client = PlateRecognizerClient(
            "token", session=session, authorised=lambda: {"12D3456"},
            cloud_breaker=self.breaker, sleep=lambda seconds: None,
        )
        self.addCleanup(client.close)
        with mock.patch.object(client, "_create_session", return_value=session):
            with self.assertRaises(requests.exceptions.ConnectionError):
                client.recognise(self.path)
            self.assertEqual(len(session.calls), 2)
            self.assertEqual(self.breaker.state, "closed")
            with self.assertLogs("gate_controller.ocr", level="INFO") as logs:
                observation = client.recognise(self.path)

        self.assertEqual(len(session.calls), 3, "the retry was removed, not sent")
        self.assertEqual(self.breaker.state, "open")
        self.assertIsNone(observation.plate)
        self.assertIn("gate_ocr stage=cloud_skipped reason=cloud_unreachable", "\n".join(logs.output))

    def test_the_predicate_the_processor_hands_down_names_the_breaker_not_the_link(self):
        session = harness_session()
        client = self._client(session)
        self._open(client, session)
        available = CloudAvailability(self.breaker, lambda: True)
        self.assertFalse(available())
        self.assertEqual(available.reason, "cloud_unreachable")

        with self.assertLogs("gate_controller.ocr", level="INFO") as logs:
            client.recognise(self.path, internet_reachable=available)

        self.assertIn("gate_ocr stage=cloud_skipped reason=cloud_unreachable", "\n".join(logs.output))
        self.assertNotIn("internet_down", "\n".join(logs.output))

    def test_a_trial_abandoned_before_it_leaves_is_released(self):
        from gate_controller.ocr import OcrResponseError

        session = harness_session()
        client = self._client(session)
        self._open(client, session)
        self.clock.advance(BREAKER_OPEN_SECONDS + 1.0)

        # The pacing wait is where an abandoned request gives up; make the
        # client wait, and abandon it while it does. Abandoning drops the
        # pooled session, so the fake is handed back as the fresh one.
        client._not_before = client._clock() + 10.0
        original_sleep = client._sleep

        def abandon_during_wait(seconds):
            client.abandon_in_flight()

        client._sleep = abandon_during_wait
        with mock.patch.object(client, "_create_session", return_value=session):
            try:
                with self.assertRaises(OcrResponseError) as caught:
                    client.recognise(self.path)
            finally:
                client._sleep = original_sleep
            client._not_before = None
            self.assertEqual(caught.exception.failure_cause, "request_abandoned")

            self.assertEqual(self.breaker.state, "half_open")
            self.assertTrue(self.breaker.available(), "the trial was released, not left claimed")
            posted = len(session.calls)
            client.recognise(self.path)
        self.assertEqual(len(session.calls), posted + 1, "the next request is the trial")
        self.assertEqual(self.breaker.state, "closed")


def harness_session():
    """A session that answers a plate until told to raise."""
    session = FakeSession([FakeResponse(cloud_payload("12D3456"))] * 20)
    session.error = None
    original = session.post

    def post(*args, **kwargs):
        if session.error is not None:
            session.calls.append(kwargs)
            raise session.error
        return original(*args, **kwargs)

    session.post = post
    return session


class CloudBreakerStateTests(unittest.TestCase):
    """The state machine on its own, on a clock the test holds."""

    def setUp(self):
        self.now = {"value": 1000.0}
        self.wall = datetime(2026, 9, 22, 15, 18, tzinfo=timezone.utc)
        self.breaker = CloudBreaker(
            clock=lambda: self.now["value"], wall_clock=lambda: self.wall,
        )

    def _die(self, times=1):
        for _ in range(times):
            self.breaker.record_outcome(requests.exceptions.ReadTimeout("lossy"))

    def test_the_threshold_is_three_consecutive_deaths(self):
        self._die(2)
        self.assertEqual(self.breaker.state, "closed")
        self.assertTrue(self.breaker.admit())
        with self.assertLogs("gate_controller.ocr", level="INFO") as logs:
            self._die()
        self.assertEqual(self.breaker.state, "open")
        self.assertFalse(self.breaker.available())
        self.assertFalse(self.breaker.admit())
        self.assertEqual(logs.output, [
            "INFO:gate_controller.ocr:gate_ocr stage=cloud_breaker state=open after=3 failures for_s=60",
        ])

    def test_every_kind_of_connection_failure_counts_and_nothing_else_does(self):
        from urllib3.exceptions import MaxRetryError, NewConnectionError
        for error in (
            requests.exceptions.ConnectionError("reset"),
            requests.exceptions.ConnectTimeout("no route"),
            requests.exceptions.ReadTimeout("slow"),
            requests.exceptions.ConnectionError(
                MaxRetryError(None, "/x", reason=NewConnectionError(None, "refused")),
            ),
            TimeoutError("socket"),
        ):
            with self.subTest(error=type(error).__name__):
                self.breaker = CloudBreaker(clock=lambda: self.now["value"])
                for _ in range(3):
                    self.breaker.record_outcome(error)
                self.assertEqual(self.breaker.state, "open")
        for error in (
            requests.exceptions.HTTPError("500"), RuntimeError("bug"),
            ValueError("bad json"), requests.exceptions.InvalidURL("x"),
        ):
            with self.subTest(error=type(error).__name__):
                self.breaker = CloudBreaker(clock=lambda: self.now["value"])
                for _ in range(5):
                    self.breaker.record_outcome(error)
                self.assertEqual(self.breaker.state, "closed")
                self.assertEqual(self.breaker.status()["failures"], 0)

    def test_deaths_further_apart_than_the_window_do_not_add_up(self):
        self._die(2)
        self.now["value"] += 301.0
        self._die()
        self.assertEqual(self.breaker.state, "closed")
        self.assertEqual(self.breaker.status()["failures"], 1)

    def test_any_answer_resets_the_count(self):
        self._die(2)
        self.breaker.record_outcome(None)
        self._die(2)
        self.assertEqual(self.breaker.state, "closed")

    def test_open_runs_out_into_half_open_where_exactly_one_request_is_admitted(self):
        self._die(3)
        self.now["value"] += 59.0
        self.assertFalse(self.breaker.available())
        self.now["value"] += 1.0
        self.assertTrue(self.breaker.available(), "read-only: nothing is claimed")
        self.assertTrue(self.breaker.available())
        self.assertEqual(self.breaker.state, "open", "available() moved nothing")
        with self.assertLogs("gate_controller.ocr", level="INFO") as logs:
            self.assertTrue(self.breaker.admit())
        self.assertEqual(self.breaker.state, "half_open")
        self.assertEqual(logs.output, [
            "INFO:gate_controller.ocr:gate_ocr stage=cloud_breaker state=half_open after=3 failures for_s=60",
        ])
        self.assertFalse(self.breaker.admit(), "the trial is out")
        self.assertFalse(self.breaker.available())
        self.breaker.abandon_trial()
        self.assertTrue(self.breaker.available())
        self.assertEqual(self.breaker.state, "half_open", "releasing is not a transition")
        self.assertTrue(self.breaker.admit())

    def test_a_trial_that_dies_reopens_for_twice_as_long_up_to_the_cap(self):
        self._die(3)
        expected = [120.0, 240.0, 480.0, 600.0, 600.0]
        for open_seconds in expected:
            self.now["value"] += BREAKER_OPEN_MAX_SECONDS + 1.0
            self.assertTrue(self.breaker.admit())
            with self.assertLogs("gate_controller.ocr", level="INFO") as logs:
                self._die()
            self.assertEqual(self.breaker.state, "open")
            self.assertEqual(self.breaker.status()["open_seconds"], open_seconds)
            self.assertTrue(logs.output[-1].endswith(f"for_s={open_seconds:.0f}"), logs.output)
        self.now["value"] += BREAKER_OPEN_MAX_SECONDS + 1.0
        self.assertTrue(self.breaker.admit())
        self.breaker.record_outcome(None)
        self.assertEqual(self.breaker.state, "closed")
        self.assertEqual(self.breaker.status()["open_seconds"], BREAKER_OPEN_SECONDS,
                         "an answer resets the open time as well as the count")

    def test_a_trial_that_fails_for_another_reason_is_released_and_nothing_moves(self):
        self._die(3)
        self.now["value"] += 61.0
        self.assertTrue(self.breaker.admit())
        self.breaker.record_outcome(RuntimeError("the caller's own bug"))
        self.assertEqual(self.breaker.state, "half_open")
        self.assertEqual(self.breaker.status()["failures"], 3)
        self.assertTrue(self.breaker.available(), "released")

    def test_the_probe_seeing_the_link_return_closes_it_and_a_closed_one_says_nothing(self):
        self._die(3)
        with self.assertLogs("gate_controller.ocr", level="INFO") as logs:
            self.breaker.internet_restored()
        self.assertEqual(self.breaker.state, "closed")
        self.assertEqual(logs.output, [
            "INFO:gate_controller.ocr:gate_ocr stage=cloud_breaker state=closed after=3 failures "
            "for_s=0 reason=internet_restored",
        ])
        with self.assertNoLogs("gate_controller.ocr", level="INFO"):
            self.breaker.internet_restored()
            self.breaker.record_outcome(None)

    def test_status_reports_the_wall_time_it_reopens(self):
        self.assertEqual(self.breaker.status(), {
            "state": "closed", "until": None, "failures": 0, "open_seconds": 60.0,
        })
        self._die(3)
        self.now["value"] += 15.0
        status = self.breaker.status()
        self.assertEqual(status["state"], "open")
        self.assertEqual(status["until"], (self.wall + timedelta(seconds=45.0)).isoformat())
        self.assertEqual(status["failures"], 3)
        self.now["value"] += 46.0
        self.breaker.admit()
        self.assertIsNone(self.breaker.status()["until"], "only while open")

    def test_the_parameters_are_checked(self):
        for kwargs in (
            {"failure_threshold": 0}, {"failure_threshold": True},
            {"failure_window_seconds": 0}, {"open_seconds": float("nan")},
            {"max_open_seconds": 30.0}, {"open_seconds": -1.0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    CloudBreaker(**kwargs)


class CloudAvailabilityTests(unittest.TestCase):
    """The one predicate the pipeline holds: the probe's answer AND the breaker's."""

    def setUp(self):
        self.now = {"value": 1000.0}
        self.breaker = CloudBreaker(clock=lambda: self.now["value"])

    def _open(self):
        for _ in range(3):
            self.breaker.record_outcome(requests.exceptions.ReadTimeout("lossy"))

    def test_both_halves_must_agree_and_the_reason_names_the_one_that_refused(self):
        available = CloudAvailability(self.breaker, lambda: True)
        self.assertTrue(available())
        self.assertIsNone(available.reason)
        self._open()
        self.assertFalse(available())
        self.assertEqual(available.reason, "cloud_unreachable")
        down = CloudAvailability(self.breaker, lambda: False)
        self.assertFalse(down())
        self.assertEqual(down.reason, "internet_down", "the probe is asked first")

    def test_either_half_may_be_absent(self):
        self.assertTrue(CloudAvailability(None, None)())
        self.assertTrue(CloudAvailability(self.breaker, None)())
        self._open()
        self.assertFalse(CloudAvailability(self.breaker, None)())
        self.assertFalse(CloudAvailability(None, lambda: False)())

    def test_a_half_that_raises_or_hedges_answers_yes(self):
        def raising():
            raise RuntimeError("probe fell over")

        class Unreadable(CloudBreaker):
            def available(self):
                raise RuntimeError("breaker state lost")

        self.assertTrue(CloudAvailability(self.breaker, raising)())
        self.assertTrue(CloudAvailability(self.breaker, lambda: 0)())
        self.assertTrue(CloudAvailability(Unreadable(), lambda: True)())
        self.assertIsNone(CloudAvailability(Unreadable(), raising).reason)

    def test_the_predicate_is_read_only(self):
        self._open()
        self.now["value"] += 61.0
        available = CloudAvailability(self.breaker, None)
        for _ in range(3):
            self.assertTrue(available())
        self.assertEqual(self.breaker.state, "open", "asking claimed no trial")


class CloudPathGuardTests(unittest.TestCase):
    """Where the breaker is asked, in source: after the probe, before the post."""

    def _source(self, name):
        return (PACKAGE / name).read_text(encoding="utf-8")

    def test_the_request_site_asks_the_breaker_after_the_probe_and_before_the_post(self):
        source = self._source("ocr.py")
        body = source[source.index("def _recognise_once("):]
        probe = body.index('_internet_reachable(state.get("internet_reachable"))')
        breaker = body.index("self._breaker_admits()")
        post = body.index("session.post(")
        self.assertLess(probe, breaker, "claiming the trial before the probe is asked would waste it")
        self.assertLess(breaker, post, "the request leaves before the breaker is asked")

    def test_the_breaker_is_fed_by_the_post_and_by_nothing_else(self):
        source = self._source("ocr.py")
        body = source[source.index("def _recognise_once("):source.index("def _breaker_admits(")]
        self.assertEqual(body.count("self._breaker_note("), 2, "one answer, one death")
        elsewhere = source[:source.index("def _recognise_once(")]
        self.assertNotIn("record_outcome(", elsewhere.split("class PlateRecognizerClient")[-1])
        tree = ast.parse(source)
        callers = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and any(
                isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == "record_outcome"
                for call in ast.walk(node)
            )
        }
        self.assertEqual(callers, {"_breaker_note"})

    def test_the_sweep_and_the_processor_name_the_half_that_refused(self):
        sweep = self._source("trigger_capture.py")
        self.assertIn("reason=%s of=%d", sweep)
        self.assertIn("self._cloud_unavailable_reason()", sweep)
        processor = self._source("processor.py")
        self.assertIn("_unavailable_reason(self._internet_reachable)", processor)


if __name__ == "__main__":
    unittest.main()
