"""The IR illuminator's bounded lease.

Turning the IR illuminator on at night re-creates the specular return off the
near gate post and degrades plate recognition, so every change here is a bounded
lease.  The lease machinery itself -- the durable record, the revert timer, the
startup reconcile, the locking -- lives in `lease.py` and is shared with the
white spotlight; this module is only the IR light's identity.

The names re-exported here (`RevertWorker`, `DIRECT_READ_MAX_AGE_SECONDS`, the
lease bounds) are the ones the rest of the service and its tests have always
imported from `gate_camera_control.ir`.
"""

from .lease import (  # noqa: F401 - re-exported for existing importers
    BACKGROUND_REFRESH_SECONDS,
    DEFAULT_LEASE_MINUTES,
    DIRECT_READ_MAX_AGE_SECONDS,
    MAX_LEASE_CLOCK_SKEW_SECONDS,
    MAX_LEASE_FILE_BYTES,
    MAX_LEASE_MINUTES,
    OBSERVATION_MAX_AGE_SECONDS,
    REVERT_RETRY_SECONDS,
    LightLeaseController,
    RevertWorker,
)
from .reolink import IR_STATES


class IrController(LightLeaseController):
    """The IR illuminator: exactly `Auto` or `Off`, and no brightness at all."""

    STATES = IR_STATES
    LABEL = "IR"
    STAGES = {
        "set": "ir_set",
        "revert": "ir_revert",
        "startup_revert": "startup_revert",
        "startup_reconcile": "startup_reconcile",
        "lease_corrupt": "lease_corrupt",
    }

    def _read_camera(self) -> str:
        return self._client.ir_state()

    def _write_camera(self, state: str) -> None:
        self._client.set_ir_state(state)
