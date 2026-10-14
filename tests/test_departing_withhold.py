"""The relay does not pulse for a car that is leaving (gate-controller#171).

From 2026-10-10 18:26 to 2026-10-14 15:30 UTC the Pi pulsed the relay
eighteen times for vehicles, and eight of those pulses went to cars that were
*leaving*. The relay is on the TOPENS step-by-step input, so a pulse on a gate
the exit wand has opened starts it closing, and one on a moving gate stops it
dead (docs/gate-operator.md). On 2026-10-14 at 13:11 the pulse closed a gate
that had begun opening for the car eleven seconds earlier.

Every passage below is replayed from the Pi's journal -- each boxed read at
the moment it was read, at the width the detector gave it, authorised or not
as the sweep judged it, and the grant arriving the way it arrived: a sweep
frame, a cloud answer on a frame the sweep handed over (at the moment the
cloud answered on the day), or the camera's own FTP still. It all goes the
way the alarm goes, on the harness ``tests/test_sweep_pipeline.py`` built:

    on_camera_event -> run_forever -> local_sweep -> the worker's injector ->
    the burst queue -> GateProcessor.prepare -> GateProcessor.process ->
    ActuationCoordinator -> the relay

with the coordinator wired as ``main`` wires it: the departure rule's mode
and hold, and the capture's ``departure_reading`` for a grant that does not
carry its own passage. The fakes sit at the true boundaries: the frame source,
the ONNX engine (boxing each plate as wide as the journal says), the HTTP
session and the relay.
"""
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import partial
from threading import Lock
from time import monotonic, sleep
from unittest import mock

from gate_controller.actuation import DEPARTING
from gate_controller.command_server import DirectCommandExecutor
from gate_controller.departure import DEFAULT_HOLD_SECONDS, DepartureConfig
from gate_controller.local_sweep import crop_to_region
from gate_controller.trigger_capture import TriggerCaptureConfig
from tests import test_sweep_pipeline as harness
from tests.test_cloud_hold_pipeline import BoxingEngine
from tests.test_local_recognizer import FakeResponse, FakeSession, cloud_payload
from tests.test_processor import RecordingRelay
from tests.test_sweep_pipeline import REGION, CapturedLogs, Gate, digest, frame, wait_for

PLATE = "11WH2571"
JUNK = "1112571"

#: (reads, grant) for every departing pulse of 2026-10-10..14. A read is
#: (seconds after the alarm it completed, plate px, authorised by the sweep).
#: A grant is ("sweep", None) -- the sweep's own authorised frame --
#: ("cloud", seconds) -- the cloud's answer on the first frame the sweep handed
#: over, given at the moment the relay fired on the day -- or ("still", seconds),
#: the camera's FTP still, read on the device.
DEPARTURES = {
    # 131D2696, the camera's still granted before the sweep had boxed a plate;
    # then the car stood beside the camera, its plate *growing*, 181 -> 390.
    "2026-10-11 10:47": ([(9.43, 181, False), (9.68, 364, True), (9.91, 366, True),
                          (10.14, 381, True), (10.69, 390, True)], ("still", 8.52)),
    "2026-10-11 16:19": ([(1.44, 383, False), (2.19, 271, False), (3.87, 250, False),
                          (4.21, 233, False), (4.47, 195, False), (4.75, 169, False),
                          (5.0, 164, False), (5.35, 145, False), (5.64, 133, False),
                          (5.92, 116, False), (6.2, 117, False), (6.51, 116, False)],
                         ("cloud", 5.42)),
    "2026-10-12 12:59": ([(1.82, 366, False), (2.65, 259, False), (3.68, 241, False),
                          (3.96, 259, False), (4.52, 212, True), (4.84, 205, True),
                          (5.13, 181, True), (5.39, 138, False), (5.68, 157, False),
                          (6.1, 157, False)], ("cloud", 3.68)),
    "2026-10-12 15:48": ([(2.21, 278, False), (4.21, 253, False), (4.54, 229, False),
                          (4.81, 216, False), (5.1, 178, False), (5.44, 166, False),
                          (5.71, 159, False), (6.02, 140, False), (6.34, 150, False),
                          (6.64, 136, False), (6.92, 114, False), (7.18, 117, True),
                          (7.55, 95, False), (7.83, 97, False)], ("sweep", None)),
    "2026-10-13 10:49": ([(1.92, 416, False), (2.86, 409, False), (3.43, 288, True),
                          (3.75, 293, True), (4.03, 245, False), (4.31, 229, False),
                          (4.59, 210, False), (4.88, 186, False), (5.72, 150, True)],
                         ("cloud", 3.34)),
    "2026-10-13 16:08": ([(2.01, 283, False), (2.95, 186, False), (3.25, 272, False),
                          (3.55, 247, False), (3.83, 209, False), (4.11, 186, False),
                          (4.38, 169, False), (4.73, 164, False), (4.98, 147, True),
                          (5.3, 155, False), (5.57, 157, False)], ("sweep", None)),
    "2026-10-14 13:11": ([(1.45, 379, False), (2.83, 293, False), (3.95, 250, False),
                          (4.28, 267, False), (4.59, 293, False), (4.88, 247, False),
                          (5.16, 222, False), (5.78, 190, False), (6.07, 178, False),
                          (6.34, 171, False), (6.68, 150, False)], ("cloud", 5.90)),
    "2026-10-14 15:17": ([(1.47, 333, False), (3.23, 293, False), (3.6, 272, False),
                          (3.86, 260, True), (4.14, 241, True), (4.51, 236, True),
                          (4.79, 209, False), (5.04, 190, False), (5.33, 178, True),
                          (5.62, 166, False), (5.9, 162, False), (6.17, 159, False)],
                         ("sweep", None)),
}

#: Every arrival let in over the same days, the same way.
ARRIVALS = {
    "2026-10-10 19:26": ([(4.81, 233, False), (5.06, 255, False), (5.29, 305, False),
                          (5.53, 319, False), (5.76, 343, True), (5.99, 362, True)],
                         ("sweep", None)),
    # The camera's still, granted with one far plate boxed (134 px).
    "2026-10-11 12:36": ([(2.01, 134, False), (4.08, 203, True), (4.45, 160, True),
                          (4.72, 178, True)], ("still", 3.55)),
    "2026-10-11 15:08": ([(0.8, 153, False), (1.91, 164, False), (2.21, 153, False),
                          (2.53, 186, False), (3.12, 193, True), (3.41, 234, True),
                          (3.74, 231, True)], ("sweep", None)),
    "2026-10-11 16:36": ([(1.57, 203, False), (2.38, 150, False), (2.67, 181, False),
                          (2.97, 203, False), (3.55, 257, False), (3.83, 269, True),
                          (4.14, 295, True)], ("sweep", None)),
    "2026-10-11 16:53": ([(0.86, 150, False), (1.21, 153, False), (1.77, 150, False),
                          (2.28, 181, False), (2.6, 200, False), (3.14, 250, False),
                          (3.44, 243, False), (3.71, 284, True), (3.98, 378, True)],
                         ("sweep", None)),
    "2026-10-12 11:09": ([(4.6, 191, False), (4.88, 209, False), (5.18, 236, False),
                          (5.54, 291, False), (5.8, 309, False), (6.07, 357, False),
                          (6.36, 409, False), (6.65, 428, False), (6.91, 460, True),
                          (7.21, 440, True)], ("sweep", None)),
    "2026-10-12 16:42": ([(5.61, 114, False), (6.44, 157, False), (6.72, 166, False),
                          (6.99, 181, False), (7.34, 207, True), (7.66, 217, True)],
                         ("sweep", None)),
    # The cloud answered at +3.30 s; the sweep's own read of the plate came at
    # +3.06, and here, with no link to wait on, it is the one that opens.
    "2026-10-12 17:17": ([(1.53, 284, False), (2.14, 193, False), (2.49, 210, False),
                          (3.06, 293, True), (3.38, 350, True)], ("sweep", None)),
    "2026-10-13 09:52": ([(2.43, 147, False), (2.78, 184, False), (3.08, 207, False),
                          (3.35, 216, False), (3.65, 247, True), (3.96, 288, True)],
                         ("sweep", None)),
    # The arrival the two-read rule judged departing 1.1 s after its pulse:
    # the detector's box jumped between 205 and 322 px.
    "2026-10-13 13:37": ([(0.8, 110, False), (1.6, 205, True), (2.37, 322, True),
                          (2.68, 205, True), (2.96, 205, True), (3.27, 241, True),
                          (3.56, 322, True), (3.82, 359, True), (4.14, 386, True)],
                         ("sweep", None)),
}


class TimedCloud(FakeSession):
    """The cloud reader, answering the plate on the first frame it is sent,
    at the moment the cloud's answer opened the gate on the day.

    Every other upload answers "no plate" at once.
    """

    def __init__(self, gate_frames, answer_at):
        super().__init__([])
        self._frames = gate_frames
        self._answer_at = answer_at
        self._lock = Lock()
        self.answered = False

    def post(self, *args, **kwargs):
        self.calls.append(kwargs)
        with self._lock:
            first, self.answered = not self.answered, True
        if not first:
            return FakeResponse({"results": []})
        started = self._frames._started_at
        if started is not None:
            delay = started + self._answer_at - monotonic()
            if delay > 0:
                sleep(delay)
        return FakeResponse(cloud_payload(PLATE.lower()))


class PulsingRelay(RecordingRelay):
    """The relay as the Pi's holds its contact: 2.0 s before the answer comes back.

    Only for shadow, where what `on` *would* have done is followed while the
    pulse is going: on the Pi the sweep keeps reading through those two
    seconds, because nothing tells it the gate opened until the pulse ends.
    """

    def trigger(self, source, idempotency_key=None, *, pre_activation_inhibit=None,
                on_activation=None):
        result = super().trigger(
            source, idempotency_key, pre_activation_inhibit=pre_activation_inhibit,
            on_activation=on_activation,
        )
        if result.activated:
            sleep(2.0)
        return result


def replay_gate(test, reads, *, mode="on", hold=DEFAULT_HOLD_SECONDS, cloud=None,
                authorised=(PLATE,), relay=None):
    """The harness's gate, with plates boxed as wide as ``reads`` says and the
    departure rule wired into the coordinator as ``main`` wires it."""
    built = {}
    options = {
        "departure": DepartureConfig(mode=mode, hold_seconds=hold),
        "departure_reading": lambda: (
            built["capture"].departure_reading() if "capture" in built else None
        ),
    }
    with mock.patch.object(
        harness, "TriggerCaptureConfig",
        partial(TriggerCaptureConfig, sweep_cloud_hold="on", sweep_cloud_min_plate_px=232,
                sweep_cloud_last_chance_seconds=1.0),
    ), mock.patch.object(
        harness, "ScriptedEngine", partial(BoxingEngine, widths={}),
    ), mock.patch.object(harness, "RecordingRelay", relay or RecordingRelay):
        gate = Gate(
            test, answers={}, cloud=cloud, authorised=set(authorised),
            sweep_seconds=max(at for at, _width, _authorised in reads) + 0.6,
            cloud_frames=5 if cloud is not None else 0,
            coordinator_options=options,
        )
    built["capture"] = gate.capture
    frames = []
    for index, (_at, width, is_authorised) in enumerate(reads):
        data = frame(5000 + index * 7 + width)
        band = digest(crop_to_region(data, REGION))
        gate.engine.widths[band] = width
        gate.engine.answers[band] = (PLATE, 0.95) if is_authorised else (JUNK, 0.30)
        frames.append(data)
    # Each frame appears just before the moment its read completed on the day.
    gate.frames.script([
        (max(0.01, at - 0.05), data) for (at, _width, _authorised), data in zip(reads, frames)
    ])
    return gate


class ReplayMixin:
    def setUp(self):
        self.gate = None

    def tearDown(self):
        if self.gate is not None:
            self.gate.close()

    def replay(self, reads, grant, *, mode="on", hold=DEFAULT_HOLD_SECONDS, relay=None):
        """Raise the alarm, deliver the grant as it came on the day, and wait
        until everything the passage put in the pipeline has been decided."""
        kind, at = grant
        cloud = TimedCloud(None, at) if kind == "cloud" else None
        self.gate = gate = replay_gate(
            self, reads, mode=mode, hold=hold, cloud=cloud, relay=relay,
        )
        if cloud is not None:
            cloud._frames = gate.frames
        still = None
        if kind == "still":
            still = frame(9000 + len(reads))
            gate.engine.answers[digest(gate.pipeline_bytes(still))] = (PLATE, 0.99)
        with CapturedLogs() as logs:
            gate.alarm()
            started = monotonic()
            if still is not None:
                wait_for(lambda: monotonic() - started >= at - 0.1, at + 1.0)
                gate.ftp_still(still)
            self.assertTrue(
                wait_for(lambda: "gate_local_sweep outcome=ended" in logs.text(), 20.0),
                logs.text(),
            )
            if still is not None:
                self.assertTrue(wait_for(lambda: gate.outcomes(), 8.0), logs.text())
            if cloud is not None:
                self.assertTrue(wait_for(lambda: cloud.answered, 8.0), logs.text())
            self._quiet(gate)
        return gate, logs.text()

    @staticmethod
    def _quiet(gate, *, settle=0.8, timeout=12.0):
        """Until no new decision has come for ``settle`` seconds: a held grant,
        a late cloud answer and the frames queued behind them all finish."""
        deadline = monotonic() + timeout
        seen, since = len(gate.outcomes()), monotonic()
        while monotonic() < deadline:
            sleep(0.05)
            now = len(gate.outcomes())
            if now != seen:
                seen, since = now, monotonic()
            elif monotonic() - since >= settle:
                return

    def rows(self):
        with closing(sqlite3.connect(self.gate.store.path)) as connection:
            return connection.execute(
                "SELECT opened, reason, authorised_plate, actuation_outcome, relay_activated_at "
                "FROM events ORDER BY id"
            ).fetchall()


class DepartingPulseReplayTests(ReplayMixin, unittest.TestCase):
    """The eight departing pulses of 2026-10-10..14, with GATE_DEPARTING_WITHHOLD=on."""

    def _assert_withheld(self, when, *, first):
        reads, grant = DEPARTURES[when]
        gate, text = self.replay(reads, grant)
        self.assertEqual(gate.relay_calls, [], f"the relay pulsed for a departing car:\n{text}")
        self.assertIn("gate_actuation departure=on outcome=withheld state=departing", text)
        if first is not None:
            self.assertIn(f"first={first}", text)
        # On record as the departure it was: a denial named `departing`, with
        # the plate, and nothing that says the gate opened for it.
        withheld = [row for row in self.rows() if row[1] == DEPARTING]
        self.assertTrue(withheld, self.rows())
        for opened, _reason, plate, outcome, relay_at in withheld:
            self.assertEqual((opened, plate, outcome, relay_at), (0, PLATE, DEPARTING, None))
        self.assertFalse(any(row[0] for row in self.rows()), self.rows())
        # The sweep kept reading rather than taking the refusal as final.
        self.assertNotIn("reason=final_departing", text)
        return gate, text

    def test_10_11_16_19_a_cloud_grant_on_a_plate_already_receding(self):
        self._assert_withheld("2026-10-11 16:19", first="departing")

    def test_10_12_12_59_a_cloud_grant_before_the_plate_had_receded_is_held_then_withheld(self):
        _gate, text = self._assert_withheld("2026-10-12 12:59", first="undecided")
        self.assertRegex(text, r"departure=on outcome=withheld .* held_ms=[1-9]\d{2,3} ")

    def test_10_12_15_48_a_sweep_frame_long_after_the_verdict(self):
        self._assert_withheld("2026-10-12 15:48", first="departing")

    def test_10_13_10_49_a_cloud_grant_two_reads_in_is_held_then_withheld(self):
        _gate, text = self._assert_withheld("2026-10-13 10:49", first="undecided")
        self.assertRegex(text, r"departure=on outcome=withheld .* held_ms=[1-9]\d{2,3} ")

    def test_10_13_16_08_a_sweep_frame_after_the_verdict(self):
        self._assert_withheld("2026-10-13 16:08", first="departing")

    def test_10_14_13_11_the_pulse_that_closed_an_opening_gate(self):
        # The cloud answered 0.12 s after the read that judged it departing:
        # held for that read, or not held at all, it is withheld either way.
        self._assert_withheld("2026-10-14 13:11", first=None)

    def test_10_14_15_17_a_sweep_frame_before_the_verdict_is_held_then_withheld(self):
        self._assert_withheld("2026-10-14 15:17", first="undecided")
        # Its later authorised reads were not handed over only to be refused
        # again: one departure, one record of it.
        self.assertEqual(
            [row[1] for row in self.rows() if row[1] == DEPARTING], [DEPARTING], self.rows(),
        )

    def test_10_11_10_47_a_departure_with_no_plate_to_measure_still_pulses(self):
        """The one the plate widths cannot see. The camera's still was granted
        before the sweep had boxed anything, and the car then stood beside the
        camera with its plate growing. With nothing to judge by, the grant
        goes exactly as before: the rule only ever withholds on evidence."""
        reads, grant = DEPARTURES["2026-10-11 10:47"]
        gate, text = self.replay(reads, grant)
        self.assertEqual(gate.relay_calls, ["relay"], text)
        self.assertNotIn("departure=on outcome=withheld", text)


class ArrivalReplayTests(ReplayMixin, unittest.TestCase):
    """Every arrival let in over the same days: let in, and never held."""

    def test_every_arrival_of_10_to_14_october_is_let_in_with_no_hold(self):
        for when, (reads, grant) in ARRIVALS.items():
            with self.subTest(arrival=when):
                gate, text = self.replay(reads, grant)
                try:
                    self.assertEqual(gate.relay_calls, ["relay"], text)
                    self.assertTrue(gate.opened(), gate.outcomes())
                    # Not withheld, and not held for a moment either: the hold
                    # journals itself whenever it waits at all.
                    self.assertNotIn("gate_actuation departure=on", text)
                    self.assertNotIn(DEPARTING, [row[1] for row in self.rows()])
                finally:
                    gate.close()
                    self.gate = None

    def test_the_10_13_13_37_arrival_is_never_judged_departing(self):
        reads, grant = ARRIVALS["2026-10-13 13:37"]
        gate, text = self.replay(reads, grant)
        self.assertEqual(gate.relay_calls, ["relay"], text)
        self.assertNotIn("stage=departing", text)
        self.assertNotIn("state=departing", text)


class WithheldArrivalStillGetsInTests(ReplayMixin, unittest.TestCase):
    def test_a_car_wrongly_judged_departing_that_stands_at_the_gate_is_let_in(self):
        """Requirement 3 of #171: a withheld car that turns out to be arriving
        must still be able to get in, by the sweep it is in.

        One alarm, two cars: a big one leaves (583 px, receding), and an
        authorised car behind it comes up and stops at the gate at 370 px --
        under 70 % of the first car's plate, so its reads count as a shrink
        and its first grant is withheld as departing. Its plate then stands
        still for two seconds, which a leaving car's never does, and the very
        next authorised read lets it in -- once.
        """
        reads = [(0.6, 583, False), (0.9, 520, False), (1.2, 470, False), (1.5, 400, False)]
        reads += [(1.8 + 0.25 * index, 370 if index % 2 else 372, True) for index in range(14)]
        gate, text = self.replay(reads, ("sweep", None))
        self.assertEqual(gate.relay_calls, ["relay"], text)
        reasons = [row[1] for row in self.rows()]
        self.assertEqual(reasons[0], DEPARTING, self.rows())
        self.assertIn("exact_match", reasons)
        self.assertIn("gate_actuation departure=on outcome=withheld", text)

    def test_a_person_can_open_the_gate_for_a_car_judged_departing(self):
        reads, grant = DEPARTURES["2026-10-13 16:08"]
        gate, _text = self.replay(reads, grant)
        self.assertEqual(gate.relay_calls, [])
        executor = DirectCommandExecutor("primary", gate.coordinator, gate.store)
        response = executor.execute({
            "controller_id": "primary", "command": "open_gate",
            "idempotency_key": "owner-at-the-app",
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
        })
        self.assertEqual(response, {"status": "completed"})
        self.assertEqual(gate.relay_calls, ["relay"])


class ModeTests(ReplayMixin, unittest.TestCase):
    def test_shadow_pulses_exactly_as_before_and_journals_what_on_would_have_done(self):
        reads, grant = DEPARTURES["2026-10-13 10:49"]
        gate, text = self.replay(reads, grant, mode="shadow", relay=PulsingRelay)
        self.assertEqual(gate.relay_calls, ["relay"], "shadow withheld a pulse")
        self.assertIn("gate_actuation departure=shadow would=hold state=undecided", text)
        # The hold `on` would have made, followed while the pulse went: the
        # reads that came during it say the car was leaving.
        self.assertIn("gate_actuation departure=shadow stage=settled would=withhold "
                      "state=departing", text)
        self.assertNotIn("departure=on", text)
        self.assertNotIn(DEPARTING, [row[1] for row in self.rows()])

    def test_shadow_says_withhold_for_a_grant_already_judged_departing(self):
        reads, grant = DEPARTURES["2026-10-14 13:11"]
        gate, text = self.replay(reads, grant, mode="shadow")
        self.assertEqual(gate.relay_calls, ["relay"])
        self.assertIn("gate_actuation departure=shadow would=withhold state=departing", text)

    def test_off_asks_nothing(self):
        reads, grant = DEPARTURES["2026-10-14 13:11"]
        gate, text = self.replay(reads, grant, mode="off")
        self.assertEqual(gate.relay_calls, ["relay"])
        self.assertNotIn("gate_actuation departure=", text)


class CameraStillTests(ReplayMixin, unittest.TestCase):
    def test_the_cameras_still_of_a_departing_car_is_withheld_by_the_passage_it_belongs_to(self):
        """The FTP still carries no passage: the coordinator asks the capture's
        current one, judged on reads since the latest alarm."""
        reads = DEPARTURES["2026-10-13 16:08"][0]
        reads = [(at, width, False) for at, width, _authorised in reads]
        gate, text = self.replay(reads, ("still", 4.9))
        self.assertEqual(gate.relay_calls, [], text)
        self.assertIn("gate_actuation departure=on outcome=withheld state=departing", text)

    def test_the_next_cars_still_is_not_judged_by_the_car_before_it(self):
        reads = [(at, width, False) for at, width, _a in DEPARTURES["2026-10-13 16:08"][0]]
        self.gate = gate = replay_gate(self, reads)
        still = frame(9100)
        gate.engine.answers[digest(gate.pipeline_bytes(still))] = (PLATE, 0.99)
        with CapturedLogs() as logs:
            gate.alarm()
            self.assertTrue(wait_for(
                lambda: (gate.capture.departure_reading() or _Unjudged()).state == DEPARTING,
                10.0,
            ), logs.text())
            # Past the capture's minimum interval: the next car's alarm, then
            # its still, while the departing car's sweep is still reading.
            sleep(0.6)
            gate.alarm()
            self.assertIsNone(gate.capture.departure_reading())
            gate.ftp_still(still)
            self.assertTrue(wait_for(lambda: gate.opened(), 8.0),
                            f"{gate.outcomes()}\n{logs.text()}")
        self.assertEqual(gate.relay_calls, ["relay"])


class _Unjudged:
    state = None


if __name__ == "__main__":
    unittest.main()
