"""2026-09-22: the router had dropped 30-70 % of packets since the day before.

The controller's own network probe said ``internet=failed`` all morning, and
still every passage handed frames to the cloud plate reader. Each hand-over
blocked about 6 s and ended ``decision_timeout``; the 09:21 passage produced
eight denial events (``decision_timeout`` / ``ocr_busy`` / ``queue_coalesced``)
that were then uploaded over the same broken link, while the on-device reads
kept working (the 11:00 passage opened on a local exact match in 5.7 s).

Everything here goes the way the alarm goes, on the harness
``tests/test_sweep_pipeline.py`` built for it:

    on_camera_event -> run_forever -> local_sweep -> the worker's injector ->
    the burst queue -> GateProcessor.prepare -> GateProcessor.process ->
    ActuationCoordinator -> the relay

inside the real ``run_worker``, with the real sweep reader, on-device
recogniser, OCR client, processor, store, coordinator -- and the real
``NetProbeWorker``, measuring a link the test switches. The fakes sit at the
true boundaries: the frame source, the ONNX engine, the HTTP session, the
relay, and the probe's ping child, TLS open and clock. The HTTP session is the
spy and the trap at once: a request that leaves is written down *and* blocks
for the six seconds the real one did, so a leak fails on the count and on the
clock.

The rule under test has two halves and both matter. While the probe has
fresh, definite evidence that the internet is down, nothing is handed to,
queued for, or posted to the cloud reader, and the passage ends on the
device's own answer without a stall. In every other case -- probe off, never
run, stale, or saying anything but ``failed`` -- the path is exactly today's,
because a probe may remove a request that would have timed out and nothing
else: it must never make the gate open more easily.
"""
import ast
import unittest
from functools import partial
from pathlib import Path
from time import monotonic, sleep
from unittest import mock

import requests

from gate_controller.local_sweep import crop_to_region
from gate_controller.net_probe import NetProbeConfig, NetProbeWorker
from gate_controller.processor import GateProcessor
from gate_controller.trigger_capture import TriggerFrameCapture
from tests import test_net_probe as probe_fakes
from tests import test_sweep_pipeline as harness
from tests.test_local_recognizer import FakeSession
from tests.test_sweep_pipeline import REGION, CapturedLogs, Gate, digest, frame, wait_for

PACKAGE = Path(__file__).resolve().parents[1] / "gate_controller"
# What the real cloud reader did over the broken link: block, then time out.
DEAD_LINK_STALL_SECONDS = 6.0


class Link:
    """The farm's uplink as the probe measures it, and the real probe on it."""

    def __init__(self, *, up: bool):
        self.up = up
        self.now = 1000.0
        self.opens = 0
        self.probe = NetProbeWorker(
            NetProbeConfig(enabled=True),
            # A fresh canned ping child per spawn, so every cycle completes.
            popen=lambda command, **kwargs: probe_fakes.FakePopen(
                probe_fakes.HEALTHY_PING.encode()
            )(command, **kwargs),
            clock=lambda: self.now,
            host_metrics=lambda **_: dict(probe_fakes.HEALTHY_METRICS),
            proc_root=Path("/nonexistent-proc"),
            sys_class_net=Path("/nonexistent-sys"),
            internet_connect=self._open,
        )

    def _open(self, host):
        self.opens += 1
        if not self.up:
            raise OSError("no route to host")
        return dict(probe_fakes.HEALTHY_INTERNET)

    def measure(self, *, after: float = 60.0) -> str:
        """One probe cycle, ``after`` seconds on from the last, as the thread would run it."""
        self.now += after
        assert self.probe.run_once(), "the probe cycle did not run"
        return self.probe.status()["hops"]["internet"]["state"]


class DeadLinkCloud(FakeSession):
    """The cloud reader over the 2026-09-22 link: a request blocks ~6 s, then times out."""

    def __init__(self, stall: float = DEAD_LINK_STALL_SECONDS):
        super().__init__([])
        self.posted_at = []
        self.stall = stall

    def post(self, *args, **kwargs):
        self.posted_at.append(monotonic())
        sleep(self.stall)
        raise requests.exceptions.ReadTimeout("simulated dead link")


class LiveCloud(FakeSession):
    """The cloud reader over a healthy link: answers at once, finds nothing."""

    def __init__(self):
        super().__init__([])
        self.posted_at = []

    def post(self, *args, **kwargs):
        self.posted_at.append(monotonic())
        return super().post(*args, **kwargs)


def probed_gate(test, link: Link | None, **options) -> Gate:
    """The harness's gate, wired to the probe the way ``__main__`` wires it.

    ``main`` hands the probe's own bound ``internet_reachable`` to the capture
    (for the sweep's hand-overs) and to the processor (which passes it on to
    the cloud client). ``None`` is a controller with the probe switched off.
    """
    predicate = None if link is None else link.probe.internet_reachable
    with mock.patch.object(
        harness, "TriggerFrameCapture",
        partial(TriggerFrameCapture, internet_reachable=predicate),
    ), mock.patch.object(
        harness, "GateProcessor", partial(GateProcessor, internet_reachable=predicate),
    ):
        return Gate(test, **options)


def unreadable_passage(seeds):
    """Frames whose plate the device can see but never read well enough.

    Every read is the authorised plate at 0.60: under every bar, so nothing
    opens, but a plate *seen*, which is exactly the frame the sweep hands to
    the cloud first (``candidate``), and what the fallback carries.
    """
    frames = [frame(seed) for seed in seeds]
    answers = {digest(crop_to_region(data, REGION)): ("10CE1990", 0.60) for data in frames}
    return frames, answers


class InternetDownPassageTests(unittest.TestCase):
    def setUp(self):
        self.gate = None

    def tearDown(self):
        if self.gate is not None:
            self.gate.close()

    def _gate(self, link, **options):
        self.gate = probed_gate(self, link, **options)
        return self.gate

    def _run_passage(self, gate, frames, *, results=1, logs):
        """One alarm, its sweep to the end, and every result it produces."""
        gate.frames.script([(0.05 + index * 0.12, data) for index, data in enumerate(frames)])
        alarm_at = monotonic()
        gate.alarm()
        self.assertTrue(
            wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text(), 10.0),
            f"the sweep never ended:\n{logs.text()}",
        )
        self.assertTrue(
            wait_for(lambda: len(gate.outcomes()) >= results, 8.0),
            f"the passage was never decided: {gate.outcomes()}\n{logs.text()}",
        )
        # Give anything else that was going to happen the chance to.
        sleep(0.4)
        return alarm_at

    def _finished_at(self, gate):
        with gate._lock:
            return max(at for at, _paths, _result in gate.results)

    # -- the rule ------------------------------------------------------------

    def test_an_unreadable_passage_asks_the_cloud_nothing_and_ends_on_the_devices_answer(self):
        """The 09:21 passage, replayed with the probe listened to.

        46 local reads, none authorised, five hand-overs each blocking 6 s,
        eight denial events. Now: the reads go on, nothing is handed over or
        posted, and the passage is one denied ``no_match`` -- the fallback
        frame, so the visit is still on record -- decided within the window.
        """
        link = Link(up=False)
        self.assertEqual(link.measure(), "failed")
        frames, answers = unreadable_passage(range(300, 312))
        cloud = DeadLinkCloud()
        gate = self._gate(link, answers=answers, cloud=cloud, sweep_seconds=1.5,
                          cloud_frames=5, fallback=1)

        with CapturedLogs() as logs:
            alarm_at = self._run_passage(gate, frames, logs=logs)

        self.assertEqual(cloud.posted_at, [], "a request left over a link the probe said was dead")
        self.assertEqual(gate.outcomes(), [(False, "no_match")],
                         "one clear denial for the passage, on the device's own answer")
        self.assertLess(
            self._finished_at(gate) - alarm_at, 1.5 + 2.0,
            "the passage stalled: a 1.5 s window should end in a decision at once",
        )
        self.assertEqual(gate.relay_calls, [])
        status = gate.sweep_status()
        self.assertEqual(status["cloud_handovers"], 0)
        self.assertEqual(status["fallbacks"], 1)
        self.assertGreaterEqual(status["reads"], 6, "the local reads did not go on")
        text = logs.text()
        self.assertEqual(
            text.count("gate_local_sweep stage=cloud_handover_skipped reason=internet_down"), 1,
            "journalled once per sweep",
        )
        self.assertNotIn("stage=cloud_handover frame=", text)
        self.assertIn("gate_ocr stage=cloud_skipped reason=internet_down", text)
        self.assertNotIn("decision_timeout", text)
        self.assertNotIn("ocr_busy", text)
        self.assertNotIn("queue_coalesced", text)

    def test_a_locally_authorised_plate_still_opens_once_with_the_internet_down(self):
        link = Link(up=False)
        link.measure()
        frames = [frame(seed) for seed in range(320, 328)]
        answers = {digest(crop_to_region(data, REGION)): ("10CE1990", 0.95) for data in frames}
        cloud = DeadLinkCloud()
        gate = self._gate(link, answers=answers, cloud=cloud, sweep_seconds=2.0,
                          cloud_frames=5, fallback=1)

        with CapturedLogs() as logs:
            gate.frames.script([(0.05 + index * 0.12, data) for index, data in enumerate(frames)])
            gate.alarm()
            self.assertTrue(wait_for(gate.opened), f"never opened: {gate.outcomes()}\n{logs.text()}")
            self.assertTrue(wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text()))

        self.assertEqual(gate.relay_calls, ["relay"], "exactly one pulse")
        self.assertEqual(cloud.posted_at, [])
        self.assertEqual(gate.opened_result().reason, "exact_match")
        self.assertEqual(gate.stored(gate.opened_result())["source"], "local")
        self.assertIn("gate_local_sweep outcome=ended reason=opened", logs.text())

    def test_the_cameras_own_still_is_decided_on_the_device_alone(self):
        """The FTP path never sees the sweep; it is gated in the processor and the client."""
        link = Link(up=False)
        link.measure()
        cloud = DeadLinkCloud()
        gate = self._gate(link, answers={}, cloud=cloud)
        still = frame(330)
        gate.engine.answers[digest(gate.pipeline_bytes(still))] = ("10CE1990", 0.60)

        # The watcher's thread is started before the harness reports ready,
        # but on macOS FSEvents begins delivering a moment later; a still
        # written inside that moment is never seen. Let it settle first.
        sleep(0.5)
        with CapturedLogs() as logs:
            landed_at = monotonic()
            gate.ftp_still(still)
            self.assertTrue(wait_for(lambda: gate.outcomes()), "the still was never decided")

        self.assertEqual(cloud.posted_at, [])
        self.assertEqual(gate.outcomes(), [(False, "no_match")])
        self.assertLess(self._finished_at(gate) - landed_at, 3.0)
        self.assertIn("gate_ocr stage=cloud_skipped reason=internet_down", logs.text())
        self.assertNotIn("decision_timeout", logs.text())

    # -- fail open towards the cloud -----------------------------------------

    def test_with_the_internet_up_the_same_passage_goes_to_the_cloud_as_before(self):
        link = Link(up=True)
        self.assertEqual(link.measure(), "ok")
        frames, answers = unreadable_passage(range(340, 352))
        cloud = LiveCloud()
        gate = self._gate(link, answers=answers, cloud=cloud, sweep_seconds=1.5,
                          cloud_frames=5, fallback=1)

        with CapturedLogs() as logs:
            self._run_passage(gate, frames, logs=logs)

        self.assertGreaterEqual(len(cloud.posted_at), 1, "the cloud was not asked")
        self.assertGreaterEqual(gate.sweep_status()["cloud_handovers"], 1)
        self.assertIn("stage=cloud_handover frame=", logs.text())
        self.assertNotIn("internet_down", logs.text())
        self.assertEqual(gate.relay_calls, [])

    def test_a_controller_without_the_probe_is_untouched(self):
        frames, answers = unreadable_passage(range(360, 372))
        cloud = LiveCloud()
        gate = self._gate(None, answers=answers, cloud=cloud, sweep_seconds=1.5,
                          cloud_frames=5, fallback=1)

        with CapturedLogs() as logs:
            self._run_passage(gate, frames, logs=logs)

        self.assertGreaterEqual(len(cloud.posted_at), 1)
        self.assertNotIn("internet_down", logs.text())

    def test_a_failure_the_probe_has_stopped_refreshing_does_not_hold_the_cloud_back(self):
        """Two cycles with no measurement and the failure is stale: today's path."""
        link = Link(up=False)
        self.assertEqual(link.measure(), "failed")
        link.now += link.probe.internet_down_freshness_seconds + 1.0
        self.assertTrue(link.probe.internet_reachable())
        frames, answers = unreadable_passage(range(380, 392))
        cloud = LiveCloud()
        gate = self._gate(link, answers=answers, cloud=cloud, sweep_seconds=1.5,
                          cloud_frames=5, fallback=1)

        with CapturedLogs() as logs:
            self._run_passage(gate, frames, logs=logs)

        self.assertGreaterEqual(len(cloud.posted_at), 1, "a stale failure kept the cloud away")
        self.assertNotIn("internet_down", logs.text())

    # -- recovery, with no restart --------------------------------------------

    def test_the_cloud_is_back_for_the_first_passage_after_the_probe_sees_the_link_return(self):
        link = Link(up=False)
        self.assertEqual(link.measure(), "failed")
        first, answers = unreadable_passage(range(400, 412))
        second, more = unreadable_passage(range(420, 432))
        answers.update(more)
        cloud = LiveCloud()
        gate = self._gate(link, answers=answers, cloud=cloud, sweep_seconds=1.5,
                          cloud_frames=5, fallback=1)

        with CapturedLogs() as logs:
            self._run_passage(gate, first, logs=logs)
            self.assertEqual(cloud.posted_at, [], "asked the cloud while the link was down")
            self.assertEqual(gate.outcomes(), [(False, "no_match")])

            # The link comes back; the probe's next cycle, one interval on,
            # notices -- a failed open is re-measured every cycle, not every
            # five minutes -- and nothing was restarted or rebuilt.
            link.up = True
            self.assertEqual(link.measure(after=60.0), "ok")
            before = len(logs.lines)
            self._run_passage(gate, second, results=2, logs=logs)

        self.assertGreaterEqual(len(cloud.posted_at), 1, "the cloud never came back")
        later = "\n".join(logs.lines[before:])
        self.assertIn("stage=cloud_handover frame=", later)
        self.assertNotIn("internet_down", later)


class CloudPathGuardTests(unittest.TestCase):
    """Where the probe is asked, in source: the same three places the permit is."""

    def _source(self, name):
        return (PACKAGE / name).read_text(encoding="utf-8")

    def test_the_request_site_asks_the_probe_after_the_permit_and_before_the_post(self):
        source = self._source("ocr.py")
        body = source[source.index("def _recognise_once("):]
        permit = body.index('_cloud_permitted(state.get("cloud_permit"))')
        probe = body.index('_internet_reachable(state.get("internet_reachable"))')
        post = body.index("session.post(")
        self.assertLess(permit, probe)
        self.assertLess(probe, post, "the request leaves before the probe is asked")

    def test_the_processor_asks_before_queueing_and_hands_the_predicate_on(self):
        source = self._source("processor.py")
        recognise = source[source.index("    def _recognise("):source.index("    def _bounded_cloud_call(")]
        self.assertLess(
            recognise.index("not _reachable(self._internet_reachable)"),
            recognise.index("self._recognise_call("),
        )
        self.assertIn('extra["internet_reachable"] = self._internet_reachable', recognise)
        needs_cloud = source[source.index("    def needs_cloud("):source.index("    def recognise_options(")]
        self.assertIn("and self.cloud_reachable", needs_cloud,
                      "a burst the cloud cannot be asked about is queued for the cloud lane")

    def test_the_sweep_withholds_its_cloud_hand_overs_but_not_the_fallback(self):
        source = self._source("trigger_capture.py")
        sweep = source[source.index("    def local_sweep("):source.index("    def _cloud_reachable(")]
        handover = sweep.index('hand_over(frame, captured_at, digest, read, source="sweep_cloud")')
        self.assertLess(sweep.index("and cloud_reachable()\n"), handover)
        fallback = sweep[sweep.index("def run_fallback("):sweep.index("while True:")]
        self.assertNotIn(
            "cloud_reachable()", fallback,
            "the fallback is what puts the passage on record; it is skipped downstream, not here",
        )

    def test_every_answer_that_is_not_a_definite_false_lets_the_request_go(self):
        # The predicate fails open at all three sites, unlike the permit,
        # which fails closed: a probe must never cost the gate the cloud
        # except on fresh evidence. The tests above prove it for the objects;
        # this pins the expression so a later "if not probe()" cannot creep in.
        for name in ("ocr.py", "processor.py", "trigger_capture.py"):
            with self.subTest(module=name):
                source = self._source(name)
                self.assertIn("() is not False", source)
                tree = ast.parse(source)
                self.assertTrue(any(
                    isinstance(node, ast.FunctionDef)
                    and node.name in {"_internet_reachable", "_reachable", "_cloud_reachable"}
                    for node in ast.walk(tree)
                ))


if __name__ == "__main__":
    unittest.main()
