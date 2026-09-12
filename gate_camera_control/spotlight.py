"""The white spotlight's bounded lease.

The fitted RLC-811A runs in forced colour night mode (`Isp.dayNight=Color`),
under which the IR illuminator changes nothing a camera in colour can use: the
night scene measures brightness 0.028 with IR Auto and 0.028 with IR off.  The
spotlight is the light that actually reaches the plate, so it is the one an
operator now needs — and for exactly that reason it is held under the same
bounded, auto-reverting, default-`Off` lease the illuminator has always had.

It is emphatically not a night default.  The PIR floodlight already lights the
stop position, and a second light on a retroreflective plate can wash it out;
see `docs/reolink-rlc-811a.md`.  This is a manual, time-limited action.
"""

from .lease import LightLeaseController
from .reolink import DEFAULT_SPOTLIGHT_BRIGHTNESS, SPOTLIGHT_STATES


class SpotlightController(LightLeaseController):
    """The white spotlight: exactly `On` or `Off`, at one configured brightness.

    Brightness is configuration, not a request parameter. A caller may light the
    gate or leave it dark; how bright it burns is a property of the installation
    that an operator sets once, in the environment file, where it is validated
    and where it cannot be raised by whoever happens to hold an app session.
    """

    STATES = SPOTLIGHT_STATES
    LABEL = "spotlight"
    STAGES = {
        "set": "spotlight_set",
        "revert": "spotlight_revert",
        "startup_revert": "spotlight_startup_revert",
        "startup_reconcile": "spotlight_startup_reconcile",
        "lease_corrupt": "spotlight_lease_corrupt",
    }

    def __init__(self, client, *, brightness=DEFAULT_SPOTLIGHT_BRIGHTNESS, **keywords):
        if (isinstance(brightness, bool) or not isinstance(brightness, int)
                or not 1 <= brightness <= 100):
            raise ValueError("spotlight brightness must be a whole number 1-100")
        self._brightness = int(brightness)
        super().__init__(client, **keywords)

    @property
    def brightness(self) -> int:
        return self._brightness

    def _read_camera(self) -> str:
        return self._client.spotlight_state()

    def _write_camera(self, state: str) -> None:
        self._client.set_spotlight_state(state, self._brightness)

    def _extra_snapshot_fields(self) -> dict:
        return {"brightness": self._brightness}

    def _extra_journal_fields(self) -> dict:
        return {"brightness": self._brightness}
