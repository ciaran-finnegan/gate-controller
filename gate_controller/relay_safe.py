import logging

from .relay import PiRelayAdapter


def force_relay_off(adapter_factory=PiRelayAdapter, *, max_attempts: int = 3) -> bool:
    """Use the smallest possible startup path to force the gate relay off."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    try:
        relay = adapter_factory()
    except Exception:
        return False
    for _ in range(max_attempts):
        try:
            relay.off()
            return True
        except Exception:
            continue
    return False


def main() -> int:
    if force_relay_off():
        return 0
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger(__name__).error("could not force the gate relay output off")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
