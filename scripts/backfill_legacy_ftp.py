"""Ship the abandoned FTP root to R2, one image at a time, then delete it.

``/home/ftp-user`` holds 2145 JPEGs written between 2024-09-08 and 2025-11-06
by a camera at 192.168.0.6 -- neither the current RLC-811A nor the RLC-810A it
replaced. They predate the R2 corpus entirely, so there is no copy of any of
them anywhere. This walks that directory, ships each image through the
controller's own corpus endpoint, and deletes the local file only once the
cloud has acknowledged the digest it stored.

Why this is a separate script and not a drop into the corpus directory
----------------------------------------------------------------------
``TrainingCorpus.pending`` offers artefacts **oldest stem first**, across the
root and the ``audio`` directory beneath it, precisely so a clip recorded
before a frame ships before it. Dropping 697 MB of 2024-dated artefacts in
there would therefore sort the entire backfill ahead of every live frame and
every gate audio clip, and at the uploader's 64 KB/s pacing -- only in quiet
gaps -- the real corpus would be stuck behind this one for days. The backfill
is not more important than the gate, so it does not get to go first.

It is also not part of the controller process. It is a one-off that runs to
completion and exits, it takes no lock the pipeline takes, and the controller
neither knows nor cares that it ran.

What it does not do
-------------------
The Pi holds no R2 credentials and does not gain any here: this posts the same
envelope, to the same ``/api/controller/corpus`` endpoint, with the same Access
credentials the controller already uses, and the Worker does the storing. The
one thing this asks of the contract that the live uploader does not is a
``source`` of ``legacy_ftp``, so a training query in D1 can exclude a camera
that no longer exists from a corpus meant to teach the one that does.

Backpressure, from outside the controller
-----------------------------------------
The live uploader yields to ``ActivityGate``, which lives in the controller's
own memory and cannot be read from here. The proxy is the controller's
database: an ``events`` row inside ``--quiet-seconds`` means a vehicle is being
dealt with right now, and the backfill waits. That is a coarser signal than the
gate's, which is the right direction to be wrong in -- it stands down more
often than it needs to, never less.

Resumable by construction. Every acknowledged digest is appended to a ledger,
the ledger is read at startup, and an interrupted run repeats nothing. A file
whose digest is already in the ledger is deleted without being sent again.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep

LOGGER = logging.getLogger("backfill")

#: ``192.168.0.6_01_20240920224359458_LINE_CROSSING_DETECTION.jpg``: the camera
#: address, the channel, a local timestamp to the millisecond, and the rule
#: that fired. The timestamp is the only part worth keeping as structure; the
#: rest is recorded verbatim in the sidecar rather than interpreted.
NAME_PATTERN = re.compile(
    r"^(?P<host>[0-9.]+)_(?P<channel>\d+)_"
    r"(?P<stamp>\d{17})_(?P<rule>[A-Z_]+)\.jpg$"
)

#: The camera wrote these with its clock set to Irish local time and no zone in
#: the name. Ireland ran UTC+1 for most of the range; assuming UTC would put
#: every summer image an hour early. An hour of error on a four-year-old frame
#: changes nothing that matters, so the assumption is recorded in the sidecar
#: (``captured_at_assumed_offset``) rather than hidden.
ASSUMED_OFFSET = timedelta(hours=1)

CORPUS_SCHEMA_VERSION = 1
SIDECAR_SCHEMA_VERSION = 2
FRAME_KIND = "frame"
FRAME_MEDIA_TYPE = "image/jpeg"
SOURCE = "legacy_ftp"

DEFAULT_BYTES_PER_SECOND = 32 * 1024
DEFAULT_QUIET_SECONDS = 60.0
DEFAULT_CHUNK_BYTES = 32 * 1024
#: A single image is a few hundred kilobytes; a file far larger than any of
#: them is not one of these and is left alone rather than sent.
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="directory of legacy JPEGs")
    parser.add_argument("--env-file", type=Path, default=Path("/etc/gate-controller.env"))
    parser.add_argument("--database", type=Path,
                        default=Path("/var/lib/gate-controller/gate-controller.db"))
    parser.add_argument("--ledger", type=Path,
                        default=Path("/var/lib/gate-controller/legacy-ftp-backfill.ledger"))
    parser.add_argument("--bytes-per-second", type=int, default=DEFAULT_BYTES_PER_SECOND)
    parser.add_argument("--quiet-seconds", type=float, default=DEFAULT_QUIET_SECONDS)
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after this many images (a first pass of 1 proves the contract)")
    parser.add_argument("--delete", action="store_true",
                        help="delete each local file once its digest is acknowledged")
    parser.add_argument("--dry-run", action="store_true",
                        help="build every envelope and send none")
    parser.add_argument("--max-failures", type=int, default=5,
                        help="stop once this many uploads have been refused")
    return parser.parse_args(argv)


def read_environment(path: Path) -> dict:
    """The controller's own environment file, as systemd reads it."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def captured_at(name: str) -> tuple[str, dict]:
    """The capture time from the filename, and what the name said verbatim."""
    match = NAME_PATTERN.match(name)
    if match is None:
        return datetime.now(timezone.utc).isoformat(), {"filename": name, "parsed": False}
    stamp = match.group("stamp")
    local = datetime(
        int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]),
        int(stamp[8:10]), int(stamp[10:12]), int(stamp[12:14]),
        int(stamp[14:17]) * 1000,
    )
    moment = (local - ASSUMED_OFFSET).replace(tzinfo=timezone.utc)
    return moment.isoformat(), {
        "filename": name,
        "parsed": True,
        "camera_host": match.group("host"),
        "channel": match.group("channel"),
        "rule": match.group("rule"),
        "captured_at_assumed_offset": "+01:00",
    }


def build_document(path: Path, payload: bytes, controller_id: str) -> dict:
    moment, origin = captured_at(path.name)
    digest = hashlib.sha256(payload).hexdigest()
    sidecar = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "captured_at": moment,
        "source": SOURCE,
        "artefact": {"kind": FRAME_KIND, "media_type": FRAME_MEDIA_TYPE},
        # No OCR ever ran on these and none is invented here. A training set
        # cannot afford a pseudo-label that nothing produced.
        "ocr": None,
        "extra": {"backfill": origin},
    }
    return {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "controller_id": controller_id,
        # Said out loud, because it is true and because the contract refuses a
        # capture time older than 400 days from anything that has not said it.
        # The alternative -- re-stamping a 2023 image to look recent -- would
        # file it under the wrong day in R2 and put a false date in the corpus.
        "backfill": True,
        "artefact_id": digest,
        "kind": FRAME_KIND,
        "media_type": FRAME_MEDIA_TYPE,
        "captured_at": moment,
        "bytes": len(payload),
        "sha256": digest,
        "source": SOURCE,
        "decision": None,
        "plate": None,
        "score": None,
        "sidecar": sidecar,
        "data_base64": base64.b64encode(payload).decode("ascii"),
    }


class Ledger:
    """Digests the cloud has confirmed, so an interrupted run repeats nothing."""

    def __init__(self, path: Path):
        self._path = path
        self._seen = set()
        if path.exists():
            self._seen = {
                line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }

    def __contains__(self, digest: str) -> bool:
        return digest in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def record(self, digest: str) -> None:
        self._seen.add(digest)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(f"{digest}\n")
            handle.flush()
            os.fsync(handle.fileno())


class GateActivity:
    """Whether the controller has dealt with a vehicle recently.

    Read-only and best effort: a database that cannot be opened returns "busy"
    so an unreadable signal parks the backfill rather than licensing it.
    """

    def __init__(self, path: Path, quiet_seconds: float):
        self._path = path
        self._quiet = quiet_seconds

    def busy(self) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._quiet)
        try:
            connection = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True, timeout=2.0)
            try:
                row = connection.execute(
                    "SELECT count(*) FROM events WHERE received_at >= ?",
                    (cutoff.isoformat(),),
                ).fetchone()
            finally:
                connection.close()
        except Exception:
            LOGGER.warning("activity_unreadable treating=busy", exc_info=True)
            return True
        return bool(row and row[0])


def paced_chunks(payload: bytes, *, bytes_per_second: int, chunk_bytes: int):
    """The body, metered to a fraction of the uplink.

    The same shape as the live uploader's ``PacedBody`` and for the same
    reason: the average rate stays low and the transfer has somewhere to
    notice, every few kilobytes, that it should not be running.
    """
    started = monotonic()
    sent = 0
    for offset in range(0, len(payload), chunk_bytes):
        chunk = payload[offset:offset + chunk_bytes]
        yield chunk
        sent += len(chunk)
        owed = (sent / max(1, bytes_per_second)) - (monotonic() - started)
        while owed > 0:
            sleep(min(owed, 0.5))
            owed -= 0.5


def acknowledged(response, digest: str) -> bool:
    if not isinstance(response, dict):
        return False
    body = response.get("artefact") if isinstance(response.get("artefact"), dict) else response
    for key in ("artefact_id", "sha256", "id"):
        value = body.get(key)
        if isinstance(value, str) and value == digest:
            return True
    return bool(body.get("stored") or body.get("ok") or body.get("accepted"))


def main(argv=None) -> int:
    arguments = parse_arguments(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    environment = read_environment(arguments.env_file)
    # The same default the controller itself applies, so the backfill is
    # attributed to the same controller rather than a second one.
    controller_id = environment.get("GATE_CONTROLLER_ID", "").strip() or "primary"
    base_url = environment.get("GATE_CLOUDFLARE_API_URL", "").strip()
    client_id = environment.get("GATE_CLOUDFLARE_ACCESS_CLIENT_ID", "").strip()
    client_secret = environment.get("GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET", "").strip()
    if not (base_url and client_id and client_secret):
        LOGGER.error("the environment file is missing the controller's Cloudflare settings")
        return 2

    from gate_controller.cloudflare_client import CloudflareServiceClient

    client = CloudflareServiceClient(base_url, client_id, client_secret, timeout=(5, 60))
    ledger = Ledger(arguments.ledger)
    activity = GateActivity(arguments.database, arguments.quiet_seconds)

    images = sorted(p for p in arguments.source.iterdir()
                    if p.is_file() and p.suffix.lower() == ".jpg")
    LOGGER.info("backfill start images=%d already_confirmed=%d dry_run=%s",
                len(images), len(ledger), arguments.dry_run)

    sent = skipped = failed = 0
    for path in images:
        if arguments.limit is not None and sent >= arguments.limit:
            LOGGER.info("backfill stopping at limit=%d", arguments.limit)
            break
        try:
            payload = path.read_bytes()
        except OSError:
            LOGGER.warning("unreadable file=%s", path.name, exc_info=True)
            failed += 1
            continue
        if not payload or len(payload) > MAX_IMAGE_BYTES:
            LOGGER.warning("implausible size file=%s bytes=%d", path.name, len(payload))
            failed += 1
            continue

        digest = hashlib.sha256(payload).hexdigest()
        if digest in ledger:
            skipped += 1
            if arguments.delete:
                path.unlink(missing_ok=True)
            continue

        while activity.busy():
            LOGGER.info("backfill deferred reason=gate_busy")
            sleep(30.0)

        document = build_document(path, payload, controller_id)
        if arguments.dry_run:
            LOGGER.info("would send file=%s bytes=%d captured_at=%s digest=%s",
                        path.name, len(payload), document["captured_at"], digest[:12])
            sent += 1
            continue

        envelope = json.dumps(document, separators=(",", ":")).encode("utf-8")
        try:
            response = client.post_stream(
                "/api/controller/corpus",
                paced_chunks(envelope, bytes_per_second=arguments.bytes_per_second,
                             chunk_bytes=DEFAULT_CHUNK_BYTES),
                content_type="application/json",
                headers={"Idempotency-Key": f"{controller_id}:{digest}"},
                max_response_bytes=4096,
                timeout=(5, 120),
            )
        except Exception as error:
            # The status alone does not say which field the contract objected
            # to, and a refusal that repeats 2145 times is worth one look.
            detail = ""
            response = getattr(error, "response", None)
            if response is not None:
                try:
                    detail = f" body={response.text[:300]!r}"
                except Exception:
                    detail = ""
            LOGGER.warning("upload failed file=%s error=%s%s", path.name, error, detail)
            failed += 1
            if failed >= arguments.max_failures:
                LOGGER.error("backfill stopping after %d consecutive refusals", failed)
                break
            sleep(5.0)
            continue

        if not acknowledged(response, digest):
            LOGGER.warning("unconfirmed file=%s response=%s", path.name,
                           json.dumps(response)[:200])
            failed += 1
            continue

        ledger.record(digest)
        sent += 1
        if arguments.delete:
            path.unlink(missing_ok=True)
        if sent % 25 == 0:
            LOGGER.info("backfill progress sent=%d skipped=%d failed=%d remaining=%d",
                        sent, skipped, failed, len(images) - sent - skipped - failed)

    LOGGER.info("backfill done sent=%d skipped=%d failed=%d confirmed_total=%d",
                sent, skipped, failed, len(ledger))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
