#!/usr/bin/env python3
"""Pull recorded artefacts back out of the archive, onto a machine that can work.

The controller records audio and ships it to R2, then deletes its own copy:
the card is a buffer, R2 is the archive. Everything that looks at recorded
audio -- labelling a passage, training a classifier, checking a detector
against a week of weather -- belongs on a laptop, not on a gate controller in
a cabinet. This is the way back.

    export GATE_CLOUDFLARE_API_URL=https://gate-mate.example.workers.dev
    export GATE_CLOUDFLARE_ACCESS_CLIENT_ID=...
    export GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET=...

    python3 scripts/fetch_corpus.py list --kind audio --since 2026-09-16
    python3 scripts/fetch_corpus.py fetch --kind audio --since 2026-09-16 --out ./audio

The credentials are the controller's own Access service token, read from the
environment and sent as headers. They are never logged, never written into a
filename, and never put in a URL.

Files are named by the instant they were captured, so a directory of them
sorts into chronological order and a window around any event is found by
arithmetic -- the same reason the segments on the card are named that way.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib import error, parse, request

#: Long enough for a 2.4 MB segment on a domestic uplink, short enough that a
#: hung connection does not silently stall a fetch of a whole day.
TIMEOUT_SECONDS = 120

EXTENSIONS = {
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "image/jpeg": ".jpg",
    "application/json": ".json",
}


class Archive:
    """The corpus endpoint, and the service token that opens it."""

    def __init__(self, base_url: str, client_id: str, client_secret: str):
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "CF-Access-Client-Id": client_id,
            "CF-Access-Client-Secret": client_secret,
        }

    @classmethod
    def from_environment(cls, environment=None) -> "Archive":
        environment = os.environ if environment is None else environment
        missing = [
            name for name in (
                "GATE_CLOUDFLARE_API_URL",
                "GATE_CLOUDFLARE_ACCESS_CLIENT_ID",
                "GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET",
            )
            if not (environment.get(name) or "").strip()
        ]
        if missing:
            raise SystemExit(
                "missing credentials in the environment: " + ", ".join(missing)
                + "\nthey are the controller's own Access service token; see docs/gate-audio.md"
            )
        return cls(
            environment["GATE_CLOUDFLARE_API_URL"].strip(),
            environment["GATE_CLOUDFLARE_ACCESS_CLIENT_ID"].strip(),
            environment["GATE_CLOUDFLARE_ACCESS_CLIENT_SECRET"].strip(),
        )

    def _open(self, path: str, query: dict | None = None):
        url = self.base_url + path
        if query:
            url += "?" + parse.urlencode({k: v for k, v in query.items() if v})
        try:
            return request.urlopen(
                request.Request(url, headers=self._headers), timeout=TIMEOUT_SECONDS,
            )
        except error.HTTPError as failure:
            # The body of an error can carry the token back in some proxies'
            # diagnostics, so only the status is reported.
            raise SystemExit(f"{path}: HTTP {failure.code}") from None
        except error.URLError as failure:
            raise SystemExit(f"{path}: {failure.reason}") from None

    def list(self, *, kind=None, since=None, until=None, limit=None) -> list[dict]:
        with self._open("/api/controller/corpus", {
            "kind": kind, "since": since, "until": until, "limit": limit,
        }) as response:
            return json.loads(response.read()).get("artefacts", [])

    def read(self, artefact_id: str, *, sidecar: bool = False) -> bytes:
        suffix = "/sidecar" if sidecar else ""
        with self._open(f"/api/controller/corpus/{artefact_id}{suffix}") as response:
            return response.read()


def _filename(artefact: dict) -> str:
    """`20260916T192325Z-3fa9c1.aac`: sortable, and unique on the digest."""
    stamp = str(artefact.get("captured_at", "")).replace("-", "").replace(":", "")
    stamp = stamp.split(".")[0].replace("+0000", "").rstrip("Z") + "Z"
    extension = EXTENSIONS.get(artefact.get("media_type", ""), ".bin")
    return f"{stamp}-{artefact['artefact_id'][:6]}{extension}"


def _human(count: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024.0
    return f"{count:.1f} GB"


def command_list(archive: Archive, args) -> int:
    artefacts = archive.list(
        kind=args.kind, since=args.since, until=args.until, limit=args.limit,
    )
    total = sum(int(a.get("byte_size") or 0) for a in artefacts)
    for artefact in artefacts:
        print(f"{artefact.get('captured_at', ''):<28}{artefact.get('kind', ''):<8}"
              f"{_human(int(artefact.get('byte_size') or 0)):>10}  {artefact['artefact_id'][:12]}")
    print(f"\n{len(artefacts)} artefacts, {_human(total)}")
    return 0


def command_fetch(archive: Archive, args) -> int:
    artefacts = archive.list(
        kind=args.kind, since=args.since, until=args.until, limit=args.limit,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fetched = skipped = 0
    for artefact in artefacts:
        target = out / _filename(artefact)
        if target.exists():
            # Fetching the same window twice is normal -- a day is cut into
            # overlapping windows -- and re-downloading what is already here
            # is the slowest part of it.
            skipped += 1
            continue
        target.write_bytes(archive.read(artefact["artefact_id"]))
        if args.sidecars:
            target.with_suffix(".json").write_bytes(
                archive.read(artefact["artefact_id"], sidecar=True))
        fetched += 1
        print(f"  {target.name}", flush=True)
    print(f"\n{fetched} fetched, {skipped} already here, into {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "fetch"):
        child = sub.add_parser(name)
        child.add_argument("--kind", default="audio", help="audio, frame, or omit for both")
        child.add_argument("--since", help="ISO instant or date, inclusive")
        child.add_argument("--until", help="ISO instant or date, inclusive")
        child.add_argument("--limit", type=int, default=500)
        if name == "fetch":
            child.add_argument("--out", required=True, help="directory to write into")
            child.add_argument("--sidecars", action="store_true",
                               help="also fetch each artefact's JSON sidecar")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    archive = Archive.from_environment()
    return (command_list if args.command == "list" else command_fetch)(archive, args)


if __name__ == "__main__":
    sys.exit(main())
