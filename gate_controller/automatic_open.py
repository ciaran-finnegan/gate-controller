"""The owner's switch against automatic opening, and how it is read.

``GATE_AUTOMATIC_OPEN=on|off`` (default ``on``). While it is ``off`` the
coordinator refuses every grant that did not come from a person -- sweep
frames, the camera's FTP still, presence frames, early sweeps, farm machinery
-- and records each as ``actuation_outcome="automatic_paused"``. A person's
command from the app is untouched: it keeps its own 20 s cooldown and its
role check, exactly as before.

The app can set the same switch through the settings envelope the controller
already polls (``automatic_open: {"enabled": bool}``, see
docs/plate-matching.md, "Settings Channel"); the environment is the fallback
for a board with no cloud settings. A malformed value -- in the environment
or in the envelope -- means *no change*: the environment's default stands,
and the envelope leaves the environment's value in force. Nothing here
raises, and nothing here can touch the plate-matching schedule that shares
the envelope.

This is a switch that can only withhold pulses. It never adds one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

ENV = "GATE_AUTOMATIC_OPEN"
#: The envelope key the Gate Mate Worker will write.
SETTINGS_KEY = "automatic_open"
DEFAULT_ENABLED = True

_ON = frozenset({"on", "1", "true", "yes"})
_OFF = frozenset({"off", "0", "false", "no"})


@dataclass(frozen=True)
class AutomaticOpenConfig:
    enabled: bool = DEFAULT_ENABLED
    #: ``default``, ``environment`` or ``app``: where the value came from, so
    #: the dashboard can tell a setting the owner chose from a fallback.
    source: str = "default"


def load_config(environment) -> AutomaticOpenConfig:
    """The board's own setting, from its environment. Never raises.

    An unreadable value is rejected whole, logged at ERROR and replaced by
    the shipped default (``on``): the switch exists to pause the gate on
    purpose, and a typo is not a purpose.
    """
    environment = environment or {}
    raw = str(environment.get(ENV, "") or "").strip().lower()
    if not raw:
        return AutomaticOpenConfig()
    if raw in _OFF:
        return AutomaticOpenConfig(enabled=False, source="environment")
    if raw in _ON:
        return AutomaticOpenConfig(enabled=True, source="environment")
    LOGGER.error(
        "automatic_open key=%s status=rejected using_default=%s",
        ENV, "on" if DEFAULT_ENABLED else "off",
    )
    return AutomaticOpenConfig()


def is_valid_section(section) -> bool:
    """Whether an envelope's ``automatic_open`` object is one this build can read."""
    return isinstance(section, dict) and isinstance(section.get("enabled"), bool)


def config_from_settings(section, fallback: AutomaticOpenConfig) -> AutomaticOpenConfig:
    """The owner's switch from the settings envelope, if valid, else ``fallback``.

    ``section`` is the envelope's ``automatic_open`` object. Only a strict
    boolean ``enabled`` is accepted; anything missing or malformed leaves the
    fallback in force. (The settings cache never adopts a malformed section in
    the first place -- it keeps the previous one -- so this is the second
    line.) Nothing here raises.
    """
    if not is_valid_section(section):
        return fallback
    return AutomaticOpenConfig(enabled=section["enabled"], source="app")
