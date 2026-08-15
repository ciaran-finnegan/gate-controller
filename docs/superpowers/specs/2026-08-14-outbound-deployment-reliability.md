# Outbound Deployment Reliability Design

## Goal

Allow a Raspberry Pi to adopt successful `master` releases without accepting an
inbound connection or depending on Tailscale, while preserving the currently
running gate controller whenever GitHub, networking, staging, activation, or
health verification fails.

## Safety Boundary

Production gate operation is independent of this updater. A root-run systemd
timer starts a short-lived updater at five-minute intervals. The updater only
makes outbound HTTPS and Git requests, and it never writes to
`/var/lib/gate-controller` or `/etc/gate-controller.env`.

The updater reads the exact commit at the configured branch (default `master`),
then accepts it only when the
repository's `ci.yml` workflow has a completed, successful `push` run for that
same SHA on that branch. Missing, pending, malformed, failed, rate-limited, or
unreachable API responses defer the update and leave the active release alone.

## Release Layout

- `/opt/gate-controller-deploy/releases/<40-character-sha>` contains immutable
  application source and a release-local `.venv`.
- `/opt/gate-controller-deploy/current` is an atomically replaced symlink to the
  active release.
- `/var/lib/gate-controller` remains the only writable application state.
- `/etc/gate-controller.env` remains the application configuration and secret
  store.
- `/etc/gate-controller-updater.env` is an optional root-only updater
  configuration file. A GitHub token is supported for future private-repository
  use but is not required for the current public repository.

The legacy `/opt/gate-controller` checkout is not deleted or overwritten by
bootstrap. The installer stages its exact clean Git commit into the new layout,
migrates the legacy SQLite database only when the persistent destination does
not already exist, and retains the old checkout for manual recovery.

## Update Flow

1. systemd serializes runs with `flock`.
2. Query the public GitHub API for the immutable SHA currently at the configured branch.
3. Return without changes when that SHA is already active.
4. Query workflow runs for `.github/workflows/ci.yml` and require an exact SHA,
   configured branch, `push`, `completed`, `success` match.
5. Fetch that exact SHA with Git into a temporary release directory and verify
   `HEAD` equals the requested SHA.
6. Remove Git metadata, create a release-local virtual environment, install the
   committed requirements, run all unit tests, compile Python, validate shell
   syntax, and validate systemd units before activation.
7. Switch `current` atomically, restart the fixed controller service, and require
   it to remain active throughout a bounded health window. Fixed unit/helper
   changes require an explicit bootstrap refresh.
8. On activation or health failure, restore the previous symlink, restart the
   old release, and report failure.
9. Only after successful activation, remove old SHA-named release directories
   while retaining the active release and at least two rollback candidates.

Any incomplete staging directory is outside `current` and can be removed on a
later run without affecting gate operation.

## CI And Branch Protection

GitHub Actions runs on pull requests, merge queues, and pushes to `master` with
read-only repository permissions. Stable checks cover Python 3.10, Python 3.11,
the complete `unittest` suite, byte compilation, and shell syntax. Repository
branch protection must require the named CI checks before merge. The updater
also independently requires the entire `ci.yml` push workflow to succeed for
the exact deployed SHA.

## Tailscale

Tailscale is optional break-glass access for diagnostics and manual recovery.
Neither CI, polling, release installation, health verification, rollback, nor
normal gate operation requires it.
