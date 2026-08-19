#!/usr/bin/env python3
"""CI-gated release updater for the Raspberry Pi gate controller."""

from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence


CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
GITHUB_API_ROOT = "https://api.github.com"
MAX_API_RESPONSE_BYTES = 5 * 1024 * 1024
LOGGER = logging.getLogger("gate-controller-updater")
BUILD_USER = "gate-controller-build"
LEGACY_COMMAND_SERVICE = "gate-command-server.service"


class UpdateError(RuntimeError):
    """An update failed without invalidating the currently active release."""


class ActivationError(UpdateError):
    """Activation failed and rollback was attempted."""


class UpdateDecision(Enum):
    NO_CHANGE = "no_change"
    DEFER = "defer"
    INSTALL = "install"


@dataclass(frozen=True)
class ReleaseEntry:
    sha: str
    modified_at: float


@dataclass(frozen=True)
class PendingActivation:
    candidate_sha: str
    previous_sha: str
    legacy_command_enabled: bool | None = None
    legacy_command_active: bool | None = None


@dataclass(frozen=True)
class LegacyCommandState:
    enabled: bool
    active: bool


@dataclass(frozen=True)
class UpdateConfig:
    repository: str
    branch: str
    install_root: Path
    service_name: str
    keep_releases: int
    request_timeout_seconds: int
    command_timeout_seconds: int
    health_seconds: int
    github_token: str | None

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "UpdateConfig":
        repository = values.get(
            "GATE_UPDATE_REPOSITORY", "ciaran-finnegan/gate-controller"
        )
        if REPOSITORY_PATTERN.fullmatch(repository) is None:
            raise ValueError("GATE_UPDATE_REPOSITORY must be owner/repository")
        branch = values.get("GATE_UPDATE_BRANCH", "master")
        if (BRANCH_PATTERN.fullmatch(branch) is None or ".." in branch
                or branch.endswith("/") or "//" in branch):
            raise ValueError("GATE_UPDATE_BRANCH is invalid")

        root_text = values.get("GATE_UPDATE_ROOT", "/opt/gate-controller-deploy")
        if root_text != "/opt/gate-controller-deploy":
            raise ValueError("GATE_UPDATE_ROOT is fixed by the installed systemd units")
        install_root = Path(root_text)
        if not install_root.is_absolute():
            raise ValueError("GATE_UPDATE_ROOT must be absolute")
        install_root = Path(os.path.abspath(install_root))
        protected_roots = (Path("/var/lib/gate-controller"), Path("/etc"))
        if install_root == Path("/") or any(
            install_root == protected or protected in install_root.parents
            for protected in protected_roots
        ):
            raise ValueError("GATE_UPDATE_ROOT overlaps persistent state or config")

        keep_releases = _bounded_integer(
            values, "GATE_UPDATE_KEEP_RELEASES", default=3, minimum=3, maximum=8
        )
        request_timeout = _bounded_integer(
            values, "GATE_UPDATE_REQUEST_TIMEOUT_SECONDS", 20, 5, 120
        )
        command_timeout = _bounded_integer(
            values, "GATE_UPDATE_COMMAND_TIMEOUT_SECONDS", 900, 60, 3600
        )
        health_seconds = _bounded_integer(
            values, "GATE_UPDATE_HEALTH_SECONDS", 15, 5, 120
        )
        service_name = values.get("GATE_UPDATE_SERVICE", "file-monitor.service")
        if re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", service_name) is None:
            raise ValueError("GATE_UPDATE_SERVICE must be a systemd service name")

        token = values.get("GITHUB_TOKEN") or None
        return cls(
            repository=repository,
            branch=branch,
            install_root=install_root,
            service_name=service_name,
            keep_releases=keep_releases,
            request_timeout_seconds=request_timeout,
            command_timeout_seconds=command_timeout,
            health_seconds=health_seconds,
            github_token=token,
        )

    @property
    def releases_root(self) -> Path:
        return self.install_root / "releases"

    @property
    def current_link(self) -> Path:
        return self.install_root / "current"

    @property
    def pending_activation_path(self) -> Path:
        return self.install_root / "pending-activation.json"


def _bounded_integer(
    values: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(values.get(name, str(default)))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def read_main_sha(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("branch commit response must be an object")
    sha = payload.get("sha")
    if not isinstance(sha, str) or SHA_PATTERN.fullmatch(sha) is None:
        raise ValueError("branch commit response did not contain a valid SHA")
    return sha


def has_successful_ci_run(payload: object, sha: str, branch: str = "master") -> bool:
    if SHA_PATTERN.fullmatch(sha) is None:
        raise ValueError("candidate SHA is invalid")
    if not isinstance(payload, dict):
        raise ValueError("workflow response must be an object")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise ValueError("workflow response did not contain a run list")

    return any(
        isinstance(run, dict)
        and run.get("head_sha") == sha
        and run.get("head_branch") == branch
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("path") in (CI_WORKFLOW_PATH, f"{CI_WORKFLOW_PATH}@{branch}")
        for run in runs
    )


def decide_update(
    current_sha: str | None,
    main_payload: object,
    runs_payload: object,
    branch: str = "master",
) -> UpdateDecision:
    candidate_sha = read_main_sha(main_payload)
    if current_sha == candidate_sha:
        return UpdateDecision.NO_CHANGE
    if not has_successful_ci_run(runs_payload, candidate_sha, branch):
        return UpdateDecision.DEFER
    return UpdateDecision.INSTALL


def releases_to_prune(
    entries: Sequence[ReleaseEntry],
    *,
    current_sha: str,
    keep: int,
) -> list[str]:
    if keep < 3:
        raise ValueError("the active release and two rollback releases are required")
    if SHA_PATTERN.fullmatch(current_sha) is None:
        raise ValueError("current release SHA is invalid")

    inactive = sorted(
        (entry for entry in entries if entry.sha != current_sha),
        key=lambda entry: entry.modified_at,
        reverse=True,
    )
    retained_inactive = max(0, keep - 1)
    return [entry.sha for entry in inactive[retained_inactive:]]


def discover_current_sha(current_link: Path, releases_root: Path) -> str | None:
    if not current_link.exists() and not current_link.is_symlink():
        return None
    if not current_link.is_symlink():
        raise ValueError("current release path is not a symlink")

    try:
        target = current_link.resolve(strict=True)
        resolved_releases = releases_root.resolve(strict=True)
    except OSError as error:
        raise ValueError("current release symlink is broken") from error
    if target.parent != resolved_releases or SHA_PATTERN.fullmatch(target.name) is None:
        raise ValueError("current release is outside the managed release directory")
    if not target.is_dir():
        raise ValueError("current release target is not a directory")
    return target.name


def _github_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "gate-controller-updater/1",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_json(config: UpdateConfig, endpoint: str) -> object:
    request = urllib.request.Request(
        f"{GITHUB_API_ROOT}/repos/{config.repository}/{endpoint}",
        headers=_github_headers(config.github_token),
    )
    try:
        with urllib.request.urlopen(
            request, timeout=config.request_timeout_seconds
        ) as response:
            content = response.read(MAX_API_RESPONSE_BYTES + 1)
    except (
        http.client.IncompleteRead,
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
    ) as error:
        raise UpdateError(f"GitHub API request deferred: {error}") from error
    if len(content) > MAX_API_RESPONSE_BYTES:
        raise UpdateError("GitHub API response exceeded the size limit")
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError("GitHub API returned malformed JSON") from error


def _main_commit_payload(config: UpdateConfig) -> object:
    branch = urllib.parse.quote(config.branch, safe="")
    payload = _github_json(config, f"git/ref/heads/{branch}")
    expected_ref = f"refs/heads/{config.branch}"
    if not isinstance(payload, dict) or payload.get("ref") != expected_ref:
        raise UpdateError("GitHub API returned a mismatched branch ref")
    target = payload.get("object")
    if not isinstance(target, dict) or target.get("type") != "commit":
        raise UpdateError("GitHub branch ref did not target a commit")
    sha = target.get("sha")
    if not isinstance(sha, str) or SHA_PATTERN.fullmatch(sha) is None:
        raise UpdateError("GitHub branch ref did not contain a valid commit SHA")
    return {"sha": sha}


def _workflow_runs_payload(config: UpdateConfig, sha: str) -> object:
    query = urllib.parse.urlencode(
        {
            "branch": config.branch,
            "event": "push",
            "status": "completed",
            "head_sha": sha,
            "per_page": "20",
        }
    )
    return _github_json(config, f"actions/workflows/ci.yml/runs?{query}")


def _command_environment(
    config: UpdateConfig, *, include_git_auth: bool = False
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("GITHUB_TOKEN", None)
    environment.pop("GH_TOKEN", None)
    for name in tuple(environment):
        if name.startswith("GIT_CONFIG_"):
            environment.pop(name)
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if include_git_auth and config.github_token:
        git_credentials = base64.b64encode(
            f"x-access-token:{config.github_token}".encode("utf-8")
        ).decode("ascii")
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {git_credentials}",
            }
        )
    return environment


def _run_command(
    arguments: Sequence[str | os.PathLike[str]],
    *,
    config: UpdateConfig,
    cwd: Path | None = None,
    timeout: int | None = None,
    include_git_auth: bool = False,
    run_as_user: str | None = None,
) -> str:
    command = [os.fspath(argument) for argument in arguments]
    LOGGER.info("Running %s", " ".join(command))
    try:
        process_options: dict[str, object] = {}
        if run_as_user:
            process_options.update(
                {
                    "user": run_as_user,
                    "group": run_as_user,
                    "extra_groups": (),
                    "umask": 0o022,
                }
            )
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=_command_environment(config, include_git_auth=include_git_auth),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout or config.command_timeout_seconds,
            check=True,
            **process_options,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        output = getattr(error, "stdout", None) or getattr(error, "output", None) or ""
        if output:
            LOGGER.error("Command output:\n%s", output[-8000:])
        raise UpdateError(f"command failed: {' '.join(command)}") from error
    if completed.stdout:
        LOGGER.info("Command output:\n%s", completed.stdout[-8000:])
    return completed.stdout.strip()


def verify_release(release: Path, config: UpdateConfig) -> None:
    python = release / ".venv/bin/python"
    if not python.is_file():
        _run_command(
            [sys.executable, "-m", "venv", release / ".venv"],
            config=config,
            run_as_user=BUILD_USER,
        )
        _run_command(
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "-r",
                release / "requirements.txt",
            ],
            config=config,
            cwd=release,
            run_as_user=BUILD_USER,
        )
    _run_command(
        [python, "-m", "unittest", "discover", "-s", "tests", "-v"],
        config=config,
        cwd=release,
        run_as_user=BUILD_USER,
    )
    _run_command(
        [
            python, "-m", "compileall", "-q", "gate_controller", "deployment",
            "tests", "scripts",
        ],
        config=config,
        cwd=release,
        run_as_user=BUILD_USER,
    )
    _run_command(
        ["/bin/sh", "-n", "file_monitor.sh"],
        config=config,
        cwd=release,
        run_as_user=BUILD_USER,
    )
    _run_command(
        ["/bin/bash", "-n", "deployment/install.sh"],
        config=config,
        cwd=release,
        run_as_user=BUILD_USER,
    )


def stage_release(sha: str, config: UpdateConfig) -> Path:
    if SHA_PATTERN.fullmatch(sha) is None:
        raise UpdateError("refusing to stage an invalid SHA")
    config.releases_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    destination = config.releases_root / sha
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise UpdateError("candidate release path is not a managed directory")
        verify_release(destination, config)
        return destination

    staging = Path(
        tempfile.mkdtemp(prefix=f".staging-{sha}-", dir=config.releases_root)
    )
    try:
        remote = f"https://github.com/{config.repository}.git"
        _run_command(
            ["chown", f"{BUILD_USER}:{BUILD_USER}", staging], config=config
        )
        _run_command(
            ["git", "init", "--quiet", staging],
            config=config,
            run_as_user=BUILD_USER,
        )
        _run_command(
            ["git", "-C", staging, "remote", "add", "origin", remote],
            config=config,
            run_as_user=BUILD_USER,
        )
        _run_command(
            [
                "git",
                "-C",
                staging,
                "fetch",
                "--quiet",
                "--depth=1",
                "--no-tags",
                "origin",
                sha,
            ],
            config=config,
            include_git_auth=True,
            run_as_user=BUILD_USER,
        )
        _run_command(
            ["git", "-C", staging, "checkout", "--quiet", "--detach", "FETCH_HEAD"],
            config=config,
            run_as_user=BUILD_USER,
        )
        fetched_sha = _run_command(
            ["git", "-C", staging, "rev-parse", "HEAD"],
            config=config,
            run_as_user=BUILD_USER,
        )
        if fetched_sha != sha:
            raise UpdateError("Git fetched a different commit than requested")
        shutil.rmtree(staging / ".git")
        verify_release(staging, config)
        _run_command(["chown", "-R", "root:root", staging], config=config)
        staging.chmod(0o755)
        os.rename(staging, destination)
        _fsync_directory(config.releases_root)
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_symlink(target: Path, link: Path) -> None:
    temporary = link.parent / f".{link.name}.{os.getpid()}"
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
        _fsync_directory(link.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _managed_release_path(sha: str, config: UpdateConfig) -> Path:
    try:
        releases_root = config.releases_root.resolve(strict=True)
        release = releases_root / sha
        if release.is_symlink() or not release.is_dir():
            raise ActivationError(
                "pending activation references an unmanaged release"
            )
        resolved = release.resolve(strict=True)
    except OSError as error:
        raise ActivationError("pending activation release is unavailable") from error
    if resolved.parent != releases_root:
        raise ActivationError("pending activation references an unmanaged release")
    return resolved


def _systemctl_is(state: str, service_name: str) -> bool:
    try:
        completed = subprocess.run(
            ["systemctl", state, service_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(
            f"could not determine whether {service_name} is {state}"
        ) from error
    if completed.returncode == 0:
        return True
    result = (completed.stdout or "").strip().lower()
    diagnostic = (completed.stderr or "").strip().lower()
    confirmed_not_present = {
        "is-enabled": {"disabled", "indirect", "masked", "not-found", "static"},
        "is-active": {"failed", "inactive", "not-found"},
    }
    if result in confirmed_not_present.get(state, set()):
        return False
    missing_unit_diagnostic = (
        f"failed to get unit file state for {service_name}: no such file or directory"
    )
    if state == "is-enabled" and diagnostic == missing_unit_diagnostic:
        return False
    raise UpdateError(
        f"could not determine whether {service_name} is {state}: {result or 'unknown'}"
    )


def _legacy_command_state() -> LegacyCommandState:
    return LegacyCommandState(
        enabled=_systemctl_is("is-enabled", LEGACY_COMMAND_SERVICE),
        active=_systemctl_is("is-active", LEGACY_COMMAND_SERVICE),
    )


def _retire_legacy_command_service(
    config: UpdateConfig,
    state: LegacyCommandState | None = None,
) -> LegacyCommandState:
    state = state or _legacy_command_state()
    if state.enabled or state.active:
        _run_command(
            ["systemctl", "disable", "--now", LEGACY_COMMAND_SERVICE],
            config=config,
            timeout=60,
        )
    return state


def _previous_release_expects_legacy_command_service(previous: Path) -> bool:
    return (previous / "deployment/systemd" / LEGACY_COMMAND_SERVICE).is_file()


def _restore_legacy_command_service(
    pending: PendingActivation,
    previous: Path,
    config: UpdateConfig,
) -> None:
    if (
        pending.legacy_command_enabled is None
        or pending.legacy_command_active is None
        or not _previous_release_expects_legacy_command_service(previous)
    ):
        return
    if pending.legacy_command_enabled:
        _run_command(
            ["systemctl", "enable", LEGACY_COMMAND_SERVICE],
            config=config,
            timeout=60,
        )
    if pending.legacy_command_active:
        _run_command(
            ["systemctl", "restart", LEGACY_COMMAND_SERVICE],
            config=config,
            timeout=60,
        )


def _write_pending_activation(
    release: Path,
    previous: Path,
    legacy_command_state: LegacyCommandState,
    config: UpdateConfig,
) -> PendingActivation:
    pending = PendingActivation(
        release.name,
        previous.name,
        legacy_command_state.enabled,
        legacy_command_state.active,
    )
    payload = json.dumps(
        {
            "candidate": pending.candidate_sha,
            "legacy_command_active": pending.legacy_command_active,
            "legacy_command_enabled": pending.legacy_command_enabled,
            "previous": pending.previous_sha,
            "version": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _atomic_write(config.pending_activation_path, payload, 0o600)
    return pending


def _read_pending_activation(config: UpdateConfig) -> PendingActivation | None:
    path = config.pending_activation_path
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ActivationError("pending activation record is not a regular file")
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ActivationError("pending activation record is malformed") from error
    version = payload.get("version") if isinstance(payload, dict) else None
    fields_for_version = {
        1: {"candidate", "previous", "version"},
        2: {
            "candidate",
            "legacy_command_active",
            "legacy_command_enabled",
            "previous",
            "version",
        },
    }
    if not isinstance(payload, dict) or set(payload) != fields_for_version.get(version):
        raise ActivationError("pending activation record has unexpected fields")
    if version not in fields_for_version:
        raise ActivationError("pending activation record version is unsupported")
    candidate_sha = payload["candidate"]
    previous_sha = payload["previous"]
    if (
        not isinstance(candidate_sha, str)
        or SHA_PATTERN.fullmatch(candidate_sha) is None
        or not isinstance(previous_sha, str)
        or SHA_PATTERN.fullmatch(previous_sha) is None
        or candidate_sha == previous_sha
    ):
        raise ActivationError("pending activation release SHAs are invalid")
    legacy_command_enabled = None
    legacy_command_active = None
    if version == 2:
        legacy_command_enabled = payload["legacy_command_enabled"]
        legacy_command_active = payload["legacy_command_active"]
        if not isinstance(legacy_command_enabled, bool) or not isinstance(
            legacy_command_active, bool
        ):
            raise ActivationError("pending activation legacy command state is invalid")
    pending = PendingActivation(
        candidate_sha,
        previous_sha,
        legacy_command_enabled,
        legacy_command_active,
    )
    _managed_release_path(pending.candidate_sha, config)
    _managed_release_path(pending.previous_sha, config)
    return pending


def _clear_pending_activation(config: UpdateConfig) -> None:
    config.pending_activation_path.unlink()
    _fsync_directory(config.pending_activation_path.parent)


def reconcile_pending_activation(config: UpdateConfig) -> str | None:
    pending = _read_pending_activation(config)
    current_sha = discover_current_sha(config.current_link, config.releases_root)
    if pending is None:
        return current_sha
    if current_sha == pending.previous_sha:
        previous = _managed_release_path(pending.previous_sha, config)
        _restart_and_confirm(config)
        _restore_legacy_command_service(pending, previous, config)
        _clear_pending_activation(config)
        return pending.previous_sha
    if current_sha != pending.candidate_sha:
        raise ActivationError("current release does not match pending candidate")
    try:
        if pending.legacy_command_enabled is None:
            candidate = _managed_release_path(pending.candidate_sha, config)
            previous = _managed_release_path(pending.previous_sha, config)
            pending = _write_pending_activation(
                candidate, previous, _legacy_command_state(), config
            )
        _retire_legacy_command_service(config)
        _restart_and_confirm(config)
    except Exception as candidate_error:
        previous = _managed_release_path(pending.previous_sha, config)
        try:
            _atomic_symlink(previous, config.current_link)
            _restart_and_confirm(config)
            _restore_legacy_command_service(pending, previous, config)
        except Exception as rollback_error:
            raise ActivationError(
                f"pending candidate failed health check; rollback also failed: {rollback_error}"
            ) from candidate_error
        _clear_pending_activation(config)
        raise ActivationError(
            "pending candidate failed health check and was rolled back"
        ) from candidate_error
    _clear_pending_activation(config)
    return pending.candidate_sha


def _service_is_active(config: UpdateConfig) -> bool:
    return _systemctl_is("is-active", config.service_name)


def _restart_and_confirm(config: UpdateConfig) -> None:
    _run_command(["systemctl", "restart", config.service_name], config=config, timeout=60)
    for _ in range(config.health_seconds):
        time.sleep(1)
        if not _service_is_active(config):
            raise UpdateError(
                f"{config.service_name} did not remain active during health check"
            )


def activate_release(release: Path, previous: Path, config: UpdateConfig) -> None:
    legacy_command_state = _legacy_command_state()
    pending: PendingActivation | None = None
    try:
        pending = _write_pending_activation(
            release, previous, legacy_command_state, config
        )
        _retire_legacy_command_service(
            config,
            LegacyCommandState(
                pending.legacy_command_enabled or False,
                pending.legacy_command_active or False,
            ),
        )
        _atomic_symlink(release, config.current_link)
        _restart_and_confirm(config)
        _clear_pending_activation(config)
    except Exception as activation_error:
        if pending is None:
            raise ActivationError(
                "candidate activation failed before recording rollback state"
            ) from activation_error
        LOGGER.error("Activation failed; restoring %s", previous.name)
        rollback_errors: list[str] = []
        try:
            current_sha = discover_current_sha(
                config.current_link, config.releases_root
            )
            if current_sha == release.name:
                _atomic_symlink(previous, config.current_link)
            elif current_sha != previous.name:
                raise ActivationError(
                    "activation failure left current at an unrelated release"
                )
            _restart_and_confirm(config)
            _restore_legacy_command_service(pending, previous, config)
            _clear_pending_activation(config)
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        detail = ""
        if rollback_errors:
            detail = f"; rollback also failed: {'; '.join(rollback_errors)}"
        raise ActivationError(f"candidate activation failed{detail}") from activation_error


def _release_entries(releases_root: Path) -> list[ReleaseEntry]:
    entries: list[ReleaseEntry] = []
    for path in releases_root.iterdir():
        if (
            SHA_PATTERN.fullmatch(path.name) is not None
            and path.is_dir()
            and not path.is_symlink()
        ):
            entries.append(ReleaseEntry(path.name, path.stat().st_mtime))
    return entries


def prune_releases(config: UpdateConfig, current_sha: str) -> None:
    for sha in releases_to_prune(
        _release_entries(config.releases_root),
        current_sha=current_sha,
        keep=config.keep_releases,
    ):
        release = config.releases_root / sha
        if release.parent != config.releases_root or release.is_symlink():
            raise UpdateError("refusing to remove an unmanaged release path")
        LOGGER.info("Removing old inactive release %s", sha)
        shutil.rmtree(release)


def run_once(config: UpdateConfig) -> int:
    try:
        current_sha = reconcile_pending_activation(config)
    except (UpdateError, ValueError, OSError) as error:
        LOGGER.error("Updater configuration is unsafe: %s", error)
        return 1
    if current_sha is None:
        LOGGER.error("No managed current release; run deployment/install.sh first")
        return 1

    try:
        main_payload = _main_commit_payload(config)
        candidate_sha = read_main_sha(main_payload)
        if candidate_sha == current_sha:
            LOGGER.info("Release %s is already active", current_sha)
            return 0
        runs_payload = _workflow_runs_payload(config, candidate_sha)
        decision = decide_update(current_sha, main_payload, runs_payload, config.branch)
        if decision is UpdateDecision.DEFER:
            LOGGER.info("Release %s does not yet have successful exact-SHA CI", candidate_sha)
            return 0

        candidate = stage_release(candidate_sha, config)
        previous = config.current_link.resolve(strict=True)
        activate_release(candidate, previous, config)
        try:
            prune_releases(config, candidate_sha)
        except (UpdateError, OSError) as error:
            LOGGER.warning("Release activated but old-release pruning failed: %s", error)
        LOGGER.info("Activated gate controller release %s", candidate_sha)
        return 0
    except ActivationError as error:
        LOGGER.critical("%s", error)
        return 1
    except (UpdateError, ValueError, OSError) as error:
        LOGGER.warning("Update deferred with active release unchanged: %s", error)
        return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if os.geteuid() != 0:
        LOGGER.error("The updater must run as root")
        return 1
    try:
        config = UpdateConfig.from_mapping(os.environ)
    except ValueError as error:
        LOGGER.error("Invalid updater configuration: %s", error)
        return 1
    return run_once(config)


if __name__ == "__main__":
    raise SystemExit(main())
