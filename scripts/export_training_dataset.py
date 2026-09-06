#!/usr/bin/env python3
"""Export the gate's event history and images into a local training dataset.

Reads every event with an image from the Gate Mate D1 database and pulls the
image from the private R2 bucket, both through wrangler (which must be
logged in to the account that owns gate-mate). Writes:

  <out>/manifest.jsonl   one line per event: id, timestamp, camera era,
                         plate label (if any), decision, reason, confidence,
                         fuzzy flag, image key and local path
  <out>/images/<sha>.jpg the image bytes, named by their R2 digest

Resumable: images already present are not fetched again. Nothing is written
back to the cloud. Run from a checkout of access-gate-ui with node_modules
installed, or pass --wrangler pointing at the wrangler binary.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

QUERY = (
    "select id, timestamp, plate, normalized_plate, observed_plate, authorised_plate, "
    "decision, reason, confidence, fuzzy_match, image_key from gate_events "
    "where image_key is not null order by timestamp"
)


def run_wrangler(wrangler, *args, cwd=None):
    result = subprocess.run([wrangler, *args], cwd=cwd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def load_events(wrangler, cwd):
    code, out, err = run_wrangler(wrangler, "d1", "execute", "gate-mate", "--remote", "--json",
                                  "--command", QUERY, cwd=cwd)
    if code != 0:
        sys.exit(f"d1 query failed: {err.strip()[:300]}")
    data = json.loads(out[out.index("["):]) if out.lstrip().startswith("[") else json.loads(out[out.index("{"):])
    rows = data[0]["results"] if isinstance(data, list) else data.get("results", [])
    return rows


def era_for(key: str) -> str:
    return "legacy_hikvision" if key.startswith("legacy/") else "reolink_rlc810a"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.expanduser("~/dev/gate-controller-data"))
    parser.add_argument("--wrangler", default="node_modules/.bin/wrangler")
    parser.add_argument("--cwd", default=".", help="directory containing wrangler.jsonc for gate-mate")
    parser.add_argument("--bucket", default="gate-mate-images")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    out = Path(args.out)
    images = out / "images"
    images.mkdir(parents=True, exist_ok=True)
    events = load_events(args.wrangler, args.cwd)
    if args.limit:
        events = events[:args.limit]
    print(f"{len(events)} events with images", flush=True)

    manifest = out / "manifest.jsonl"
    fetched = skipped = failed = 0
    started = time.time()
    with manifest.open("w") as target:
        for index, event in enumerate(events, 1):
            key = event["image_key"]
            digest = Path(key).stem.split("-")[-1]
            local = images / f"{digest}.jpg"
            if not local.exists() or local.stat().st_size == 0:
                code, _, err = run_wrangler(args.wrangler, "r2", "object", "get", f"{args.bucket}/{key}",
                                            "--file", str(local), "--remote", cwd=args.cwd)
                if code != 0 or not local.exists() or local.stat().st_size == 0:
                    failed += 1
                    local.unlink(missing_ok=True)
                    print(f"[{index}/{len(events)}] failed {key}: {err.strip()[:120]}", flush=True)
                    continue
                fetched += 1
            else:
                skipped += 1
            record = {
                "event_id": event["id"],
                "timestamp": event["timestamp"],
                "camera_era": era_for(key),
                "plate": event.get("plate") or None,
                "normalized_plate": event.get("normalized_plate") or None,
                "observed_plate": event.get("observed_plate") or None,
                "authorised_plate": event.get("authorised_plate") or None,
                "decision": event.get("decision"),
                "reason": event.get("reason"),
                "confidence": event.get("confidence"),
                "fuzzy_match": bool(event.get("fuzzy_match")),
                "image_key": key,
                "image": str(local.relative_to(out)),
                "hour_utc": int(event["timestamp"][11:13]) if len(event["timestamp"]) > 13 else None,
            }
            target.write(json.dumps(record, sort_keys=True) + "\n")
            if index % 50 == 0:
                rate = index / max(1.0, time.time() - started)
                print(f"[{index}/{len(events)}] fetched={fetched} cached={skipped} failed={failed} "
                      f"eta={((len(events) - index) / max(rate, 1e-6)) / 60:.0f} min", flush=True)
    print(f"done: fetched={fetched} cached={skipped} failed={failed} manifest={manifest}", flush=True)


if __name__ == "__main__":
    main()
