"""Bounded light leases that always revert, including across a service restart.

The gate camera carries two lights this service may touch — the IR illuminator
and, on the RLC-811A, a real white spotlight — and neither of them may be left
on by accident.  Turning a light on at night changes what the plate reader sees,
so every change here is a bounded lease.  The lease is persisted before the
camera is touched, so a restart in the middle of a lease still reverts to the
configured default.

The lease record lives on durable storage (systemd ``StateDirectory=``), not in
``/run``: a lease kept on tmpfs is erased by the power cut it most needs to
survive, and the camera is separately powered, so it would hold the leased state
for ever.  Because a record can still be lost -- an operator clearing the state
directory, a filesystem restored from elsewhere -- startup also *reconciles*: it
reads the camera once and puts it back to the configured default when what it
finds is not the default and no lease explains it.

Both lights share this one implementation, parameterised by their name, their
two states, their camera read and write, and their own lease file.  They do not
share a lease: each light's record names only itself, so a spotlight lease can
never revert the illuminator, and a failure on one light never cancels the
other's outstanding revert.
"""

import contextlib
import json
import math
import os
import stat
import threading
import time
from datetime import datetime, timezone

from .atomic import atomic_write
from .reolink import CameraBusy, CameraError, CameraUnreachable


DEFAULT_LEASE_MINUTES = 10
MAX_LEASE_MINUTES = 60
OBSERVATION_MAX_AGE_SECONDS = 60.0
DIRECT_READ_MAX_AGE_SECONDS = 5.0
REVERT_RETRY_SECONDS = (5.0, 10.0, 20.0, 40.0, 60.0)
MAX_LEASE_FILE_BYTES = 4 * 1024
BACKGROUND_REFRESH_SECONDS = 30.0
# A lease timestamp more than a year from now is not a lease, it is a corrupt or
# hostile record: `datetime.fromtimestamp(1e18)` raises out of `_isoformat`, and
# that exception escapes `snapshot()` -- which the state publisher calls -- so
# the heartbeat freezes at `service_unhealthy` and the revert never runs.
MAX_LEASE_CLOCK_SKEW_SECONDS = 365 * 24 * 60 * 60


class LightLeaseController:
    """Serialises every camera change for one light and owns its revert timer.

    Subclasses supply the light's identity: the two states it may hold, the two
    camera calls that read and write it, the journal stage names it uses, and
    the words its errors are phrased in.  Everything else -- the locking, the
    lease file, the retry backoff, the startup reconcile -- is shared, because
    every one of those rules exists for the same reason on both lights.
    """

    #: The exact states this light may hold; the first is not special.
    STATES = ()
    #: The word used in this light's own error messages.
    LABEL = "light"
    #: Journal stage names, spelled out rather than derived, so the IR lines
    #: stay exactly the lines the runbooks and the docs already name.
    STAGES = {
        "set": "light_set",
        "revert": "light_revert",
        "startup_revert": "startup_revert",
        "startup_reconcile": "startup_reconcile",
        "lease_corrupt": "lease_corrupt",
    }

    def __init__(self, client, *, default_state="Off", lease_path=None,
                 default_lease_minutes=DEFAULT_LEASE_MINUTES,
                 max_lease_minutes=MAX_LEASE_MINUTES, clock=time.time, journal=None):
        if default_state not in self.STATES:
            raise ValueError(
                f"the {self.LABEL} default must be "
                f"{' or '.join(self.STATES)}"
            )
        if not 1 <= default_lease_minutes <= max_lease_minutes <= MAX_LEASE_MINUTES:
            raise ValueError(f"{self.LABEL} lease bounds are invalid")
        self._client = client
        self._default_state = default_state
        self._lease_path = None if lease_path is None else os.fspath(lease_path)
        self._default_lease_minutes = int(default_lease_minutes)
        self._max_lease_minutes = int(max_lease_minutes)
        self._clock = clock
        self._journal = journal or (lambda *_arguments, **_keywords: None)
        # Two locks, and never one held across a camera call for a read.
        # `_lock` guards the published state only; `_camera_lock` serialises the
        # camera itself, with writers queueing ahead of readers.
        self._lock = threading.RLock()
        self._camera_lock = threading.Lock()
        self._writers_waiting_lock = threading.Lock()
        self._writers_waiting = 0
        self._observed_state = None
        self._observed_at = None
        self._last_error = None
        self._revert_failed = False
        self._revert_attempts = 0
        self._next_revert_attempt_at = 0.0
        self._lease, corrupt = self._load_lease()
        if corrupt:
            # A record that exists but cannot be trusted is treated as an
            # already-expired lease: the state it named is unknowable, so the
            # camera is put back to the configured default through the ordinary
            # revert path, with its retries and its journal line, rather than
            # being left wherever the unreadable record left it.
            now = self._clock()
            self._lease = {
                "state": self._default_state, "expires_at": now, "set_at": now,
            }
            self._journal(self.STAGES["lease_corrupt"], state=self._default_state)
            self._write_lease(self._lease)

    # -- the light itself: the only two camera calls a subclass supplies -------

    def _read_camera(self) -> str:
        raise NotImplementedError

    def _write_camera(self, state: str) -> None:
        raise NotImplementedError

    def _extra_snapshot_fields(self) -> dict:
        """Fields this light adds to its published document. None by default."""
        return {}

    def _extra_journal_fields(self) -> dict:
        """Fields this light adds to its own set and revert lines."""
        return {}

    # -- everything below is identical for every light ------------------------

    @property
    def default_state(self) -> str:
        return self._default_state

    @property
    def states(self) -> tuple:
        return tuple(self.STATES)

    @property
    def default_lease_minutes(self) -> int:
        return self._default_lease_minutes

    @property
    def max_lease_minutes(self) -> int:
        return self._max_lease_minutes

    def restore_default_on_start(self) -> None:
        """Put the camera back to the default before the service answers anyone.

        A lease record is the fast path: revert it and clear it.  With no record
        the camera still has to be *asked*, because the record can be lost while
        the lease it described is still in force -- the state directory wiped, a
        power cut in the days when the record lived on tmpfs -- and the camera is
        separately powered, so nothing else would ever bring it back.
        """
        with self._lock:
            outstanding = self._lease is not None
        if outstanding:
            self._journal(self.STAGES["startup_revert"], state=self._default_state)
            self._revert(stage=self.STAGES["startup_revert"])
            return
        self._reconcile_on_start()

    def _reconcile_on_start(self) -> None:
        """One bounded read, and a revert only if the camera disagrees.

        Nothing is written on the strength of a camera we could not read: an
        unobserved camera stays unobserved and is reported as `unknown`, exactly
        as it is at any other time.  A camera that answers with the configured
        default is left alone, so a restart loop still cannot become a login
        storm -- it costs one read on the cached token, never a write.
        """
        if not self.refresh_observation():
            self._journal(
                self.STAGES["startup_reconcile"], outcome="not_observed"
            )
            return
        with self._lock:
            observed = self._observed_state
            if observed is None or observed == self._default_state:
                return
            # Reconstructed as an already-expired lease so the ordinary revert
            # path owns it: the retry backoff, `revert_failed` in the heartbeat,
            # and a record that survives another restart mid-reconcile.
            now = self._clock()
            self._lease = {"state": observed, "expires_at": now, "set_at": now}
            self._write_lease(self._lease)
        self._journal(
            self.STAGES["startup_reconcile"],
            state=self._default_state, observed=observed,
        )
        self._revert(stage=self.STAGES["startup_reconcile"])

    def state(self, *, refresh_max_age=OBSERVATION_MAX_AGE_SECONDS) -> dict:
        """Return the published state, refreshing a stale observation first.

        The camera call happens outside `_lock`, single-flighted, and is skipped
        outright while a revert or a set is queued.  Readers used to make their
        own 5 s calls while holding the state lock, so a handful of concurrent
        readers could hold an expired lease's revert off the camera for over
        half a minute -- the light staying on past expiry, the exact failure the
        lease exists to prevent.
        """
        if self._observation_is_stale(refresh_max_age):
            self.refresh_observation()
        return self.snapshot()

    def refresh_observation(self) -> bool:
        """Observe the camera once, outside the state lock. True if it answered.

        Returns False without touching the camera when a writer is queued, when
        another refresh is already in flight, or when the client's breaker is
        open.  None of those is an error; each is a reason to keep what we have.
        """
        if self._writers_waiting or self._breaker_is_open():
            return False
        # Single-flight: a second reader publishes the first reader's answer
        # rather than putting a second call on the camera.
        if not self._camera_lock.acquire(blocking=False):
            return False
        try:
            state = self._read_camera()
        except CameraError as error:
            with self._lock:
                self._record_error_locked(error)
            return False
        finally:
            self._camera_lock.release()
        with self._lock:
            self._observed_state = state
            self._observed_at = self._clock()
            self._last_error = None
        return True

    def snapshot(self) -> dict:
        """Return the last known state without ever touching the camera."""
        with self._lock:
            return self._snapshot_locked()

    def set_state(self, state: str, lease_minutes=None) -> dict:
        """Apply a bounded lease to this light and return the resulting state."""
        if state not in self.STATES:
            raise ValueError(
                f"{self.LABEL} state must be {' or '.join(self.STATES)}"
            )
        minutes = self._bounded_lease_minutes(lease_minutes)
        with self._camera_writer():
            with self._lock:
                now = self._clock()
                expires_at = now + minutes * 60
                reverting = state == self._default_state
                # Persist a new lease before the camera changes so a crash
                # between the two still reverts on the next start. A cancel
                # removes nothing yet: until the camera confirms, the record is
                # the only thing that guarantees a later revert, so it is cleared
                # after the call succeeds, exactly as `_revert` does it.
                if not reverting:
                    self._write_lease({
                        "state": state, "expires_at": expires_at, "set_at": now,
                    })
            try:
                self._write_camera(state)
            except CameraError as error:
                with self._lock:
                    self._record_error_locked(error, invalidates_observation=True)
                    # A failed call is indeterminate: the camera may have applied
                    # it. Keep whichever record guarantees a later revert - the
                    # new lease when one was requested, the untouched previous
                    # lease when this was itself a revert - rather than the
                    # record that assumes success.
                    if not reverting:
                        self._lease = {
                            "state": state, "expires_at": expires_at, "set_at": now,
                        }
                    self._journal(
                        self.STAGES["set"], state=state, lease_seconds=minutes * 60,
                        outcome=error.code, **self._extra_journal_fields(),
                    )
                raise
            with self._lock:
                self._observed_state = state
                self._observed_at = now
                self._last_error = None
                if reverting:
                    self._lease = None
                    self._write_lease(None)
                    self._revert_failed = False
                    self._revert_attempts = 0
                    self._journal(
                        self.STAGES["revert"], state=state, outcome="completed",
                        **self._extra_journal_fields(),
                    )
                else:
                    self._lease = {
                        "state": state, "expires_at": expires_at, "set_at": now,
                    }
                    self._revert_failed = False
                    self._revert_attempts = 0
                    self._journal(
                        self.STAGES["set"], state=state, lease_seconds=minutes * 60,
                        outcome="completed", **self._extra_journal_fields(),
                    )
                return self._snapshot_locked()

    def revert_now(self) -> dict:
        """Cancel any lease immediately and return to the configured default."""
        return self.set_state(self._default_state, self._default_lease_minutes)

    def run_due_revert(self) -> bool:
        """Revert an expired lease, or retry a failed revert. Returns True on action."""
        with self._lock:
            if self._lease is None:
                return False
            now = self._clock()
            if self._revert_failed:
                if now < self._next_revert_attempt_at:
                    return False
            elif now < self._lease["expires_at"]:
                return False
        return self._revert(stage=self.STAGES["revert"])

    def seconds_until_next_revert(self):
        with self._lock:
            if self._lease is None:
                return None
            now = self._clock()
            due = (self._next_revert_attempt_at if self._revert_failed
                   else self._lease["expires_at"])
            return max(0.0, due - now)

    def _revert(self, *, stage: str) -> bool:
        """Put the camera back to the default. The priority path: readers wait."""
        with self._camera_writer():
            with self._lock:
                if self._lease is None:
                    return False
            try:
                self._write_camera(self._default_state)
            except CameraError as error:
                with self._lock:
                    self._record_error_locked(error, invalidates_observation=True)
                    self._revert_failed = True
                    self._revert_attempts += 1
                    backoff = REVERT_RETRY_SECONDS[
                        min(self._revert_attempts, len(REVERT_RETRY_SECONDS)) - 1
                    ]
                    self._next_revert_attempt_at = self._clock() + backoff
                    self._journal(
                        stage, state=self._default_state, outcome=error.code,
                        attempt=self._revert_attempts, retry_after=int(backoff),
                        **self._extra_journal_fields(),
                    )
                return False
            with self._lock:
                self._observed_state = self._default_state
                self._observed_at = self._clock()
                self._last_error = None
                self._revert_failed = False
                self._revert_attempts = 0
                self._lease = None
                self._write_lease(None)
                self._journal(
                    stage, state=self._default_state, outcome="completed",
                    **self._extra_journal_fields(),
                )
            return True

    @contextlib.contextmanager
    def _camera_writer(self):
        """Serialise camera writes, and hold readers off while one is queued.

        A revert that is late is the failure this module exists to prevent, so a
        waiting writer takes precedence over any read: reads see the counter and
        stand aside rather than putting another bounded call in front of it.
        """
        with self._writers_waiting_lock:
            self._writers_waiting += 1
        try:
            self._camera_lock.acquire()
        finally:
            with self._writers_waiting_lock:
                self._writers_waiting -= 1
        try:
            yield
        finally:
            self._camera_lock.release()

    def _observation_is_stale(self, max_age: float) -> bool:
        with self._lock:
            return (self._observed_at is None
                    or self._clock() - self._observed_at > max_age)

    def _breaker_is_open(self) -> bool:
        remaining = getattr(self._client, "breaker_seconds_remaining", None)
        if not callable(remaining):
            return False
        try:
            return remaining() > 0
        except Exception:
            return False

    def _record_error_locked(self, error, *, invalidates_observation=False) -> None:
        """Record a camera failure, and forget the observation it invalidated.

        Any failed *mutation* invalidates it, whatever the failure was: a plain
        `CameraError` -- a nonzero `rspCode`, an unexpected status, a body that
        is not JSON -- means the camera was asked to change and did not say
        whether it did.  Keeping the pre-change observation `fresh` published
        `ready` with the old state, so the heartbeat said `Off` while the camera
        may well have been on.  A busy or unreachable camera invalidates it
        even on a read, because neither answer says anything about the camera.
        """
        self._last_error = error.code
        if invalidates_observation or isinstance(error, (CameraBusy, CameraUnreachable)):
            self._observed_state = None
            self._observed_at = None
        self._journal(error.code, retry_after=error.retry_after)

    def _snapshot_locked(self) -> dict:
        now = self._clock()
        fresh = (self._observed_at is not None
                 and now - self._observed_at <= OBSERVATION_MAX_AGE_SECONDS)
        lease = self._lease
        remaining = None
        effective_until = None
        if lease is not None:
            remaining = max(0, math.ceil(lease["expires_at"] - now))
            effective_until = _isoformat(lease["expires_at"])
        document = {
            "state": self._observed_state if fresh else "unknown",
            "default": self._default_state,
            "effective_until": effective_until,
            "lease_seconds_remaining": remaining,
            "revert_failed": self._revert_failed,
            "last_error": self._last_error,
        }
        document.update(self._extra_snapshot_fields())
        return document

    def _bounded_lease_minutes(self, lease_minutes) -> int:
        if lease_minutes is None:
            return self._default_lease_minutes
        if isinstance(lease_minutes, bool) or not isinstance(lease_minutes, int):
            raise ValueError("lease_minutes must be a whole number of minutes")
        if not 1 <= lease_minutes <= self._max_lease_minutes:
            raise ValueError("lease_minutes is outside the configured bounds")
        return lease_minutes

    def _load_lease(self):
        """Return ``(lease, corrupt)``: no record, a usable one, or an unusable one.

        The three cases are distinct. No file means no lease. A usable record is
        reverted on start. An unusable one -- unreadable, unparseable, or naming
        a time that is not a time -- must not be silently read as "no lease",
        because the camera may still be holding whatever it described.

        A record naming a state this light cannot hold is unusable too, which is
        what keeps the two lights' files from ever being read across: an IR
        record found in the spotlight's path is corrupt to the spotlight, and is
        resolved by restoring the spotlight's own default.
        """
        if self._lease_path is None:
            return None, False
        flags = os.O_RDONLY | os.O_NONBLOCK
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._lease_path, flags)
        except FileNotFoundError:
            return None, False
        except OSError:
            return None, True
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode)
                    or not 0 < metadata.st_size <= MAX_LEASE_FILE_BYTES):
                return None, True
            body = os.read(descriptor, metadata.st_size)
        except OSError:
            return None, True
        finally:
            os.close(descriptor)
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, True
        if (not isinstance(decoded, dict)
                or set(decoded) != {"state", "expires_at", "set_at"}
                or decoded["state"] not in self.STATES):
            return None, True
        now = self._clock()
        for key in ("expires_at", "set_at"):
            value = decoded[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, True
            # Bounded to a year either side of now. Beyond that the value is not
            # a timestamp this service ever wrote, and rendering it would raise
            # out of `_isoformat` and out of `snapshot()` with it.
            if (not math.isfinite(value)
                    or abs(float(value) - now) > MAX_LEASE_CLOCK_SKEW_SECONDS):
                return None, True
        return {
            "state": decoded["state"],
            "expires_at": float(decoded["expires_at"]),
            "set_at": float(decoded["set_at"]),
        }, False

    def _write_lease(self, lease) -> None:
        if self._lease_path is None:
            return
        if lease is None:
            try:
                os.unlink(self._lease_path)
            except OSError:
                pass
            return
        body = json.dumps({
            "state": lease["state"],
            "expires_at": float(lease["expires_at"]),
            "set_at": float(lease["set_at"]),
        }, separators=(",", ":"), sort_keys=True).encode("utf-8")
        atomic_write(self._lease_path, body, 0o600)


class RevertWorker:
    """Background thread that fires due reverts and retries failed ones.

    It drives every light it is given. One thread rather than one per light:
    a revert is a sub-second camera write behind the same client, and two
    threads would only queue against each other on the same connection.
    """

    def __init__(self, *controllers, interval_seconds=1.0):
        if not controllers:
            raise ValueError("the revert worker needs at least one light")
        self._controllers = controllers
        self._interval_seconds = float(interval_seconds)
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="camera-light-revert", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=self._interval_seconds + 1)

    def _run(self) -> None:
        while not self._stopped.is_set():
            for controller in self._controllers:
                try:
                    controller.run_due_revert()
                except Exception:
                    # A revert failure is recorded on the controller and retried;
                    # it must never take the service down, and one light's
                    # failure must never stop the other light's revert.
                    pass
            self._stopped.wait(self._interval_seconds)


def _isoformat(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(
        float(epoch_seconds), tz=timezone.utc
    ).replace(microsecond=0).isoformat()
