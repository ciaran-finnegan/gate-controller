from collections.abc import Callable, Mapping
from pathlib import Path
from subprocess import DEVNULL, run


class PromptPlayer:
    """Play only prompt keys configured by the controller operator."""

    def __init__(self, prompts: Mapping[str, Path], runner: Callable[[Path], bool] | None = None):
        self._prompts = {key: Path(path) for key, path in prompts.items()}
        self._runner = runner or _play_file

    @property
    def available(self) -> bool:
        return bool(self._prompts)

    def play(self, prompt_key: str | None) -> bool:
        path = self._prompts.get(prompt_key or "")
        if path is None:
            return False
        try:
            return bool(self._runner(path))
        except Exception:
            return False


def _play_file(path: Path) -> bool:
    result = run(["aplay", str(path)], check=False, timeout=10, stdout=DEVNULL, stderr=DEVNULL)
    return result.returncode == 0
