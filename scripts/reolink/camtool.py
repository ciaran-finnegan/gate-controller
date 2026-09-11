#!/usr/bin/env python3
"""Reolink camera API helper for the gate controller Pi.

Credentials are read from the root-only /etc/gate-media-gateway.env RTSP URLs and
are never printed. A single login token is cached in /root/.camtool-token.json and
reused, because repeated Login calls make this firmware return 502 for ~1 minute.

Usage:
  camtool.py get <Cmd> [<Cmd> ...]      # GetIsp GetImage GetIrLights ...
  camtool.py raw '<json array>'         # arbitrary command list
  camtool.py snap <outfile.jpg>
  camtool.py measure <file.jpg> [<file.jpg> ...]
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request

HOST = "192.168.0.54"
ENV_FILE = "/etc/gate-media-gateway.env"
TOKEN_FILE = "/root/.camtool-token.json"
# GATE_PLATE_REGION default from /etc/gate-controller.env
PLATE_REGION = (0.05, 0.4, 0.9, 0.6)

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def credentials():
    with open(ENV_FILE, "r", encoding="utf-8") as handle:
        text = handle.read()
    match = re.search(r"rtsp://([^:@/\s]+):([^@/\s]+)@" + re.escape(HOST), text)
    if not match:
        raise SystemExit("could not parse camera credentials from env file")
    return urllib.parse.unquote(match.group(1)), urllib.parse.unquote(match.group(2))


def post(params, body, timeout=20):
    url = "https://%s/cgi-bin/api.cgi?%s" % (HOST, urllib.parse.urlencode(params))
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout, context=CTX) as response:
        return response.read()


def login():
    user, password = credentials()
    payload = [{
        "cmd": "Login",
        "param": {"User": {"Version": "0", "userName": user, "password": password}},
    }]
    raw = post({"cmd": "Login"}, payload)
    parsed = json.loads(raw)
    entry = parsed[0]
    if entry.get("code") != 0:
        raise SystemExit("login failed: %s" % json.dumps(entry.get("error", {})))
    token = entry["value"]["Token"]["name"]
    lease = int(entry["value"]["Token"].get("leaseTime", 3600))
    record = {"token": token, "expires": time.time() + lease - 120}
    with open(TOKEN_FILE, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    os.chmod(TOKEN_FILE, 0o600)
    return token


def token():
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as handle:
            record = json.load(handle)
        if record.get("expires", 0) > time.time():
            return record["token"]
    except (OSError, ValueError, KeyError):
        pass
    return login()


def call(body, retry=True):
    tok = token()
    cmd = body[0].get("cmd", "Get")
    raw = post({"cmd": cmd, "token": tok}, body)
    parsed = json.loads(raw)
    stale = any(
        entry.get("code") != 0
        and str(entry.get("error", {}).get("detail", "")).lower().find("login") >= 0
        for entry in parsed
        if isinstance(entry, dict)
    )
    if stale and retry:
        login()
        return call(body, retry=False)
    return parsed


def snap(outfile):
    tok = token()
    params = {
        "cmd": "Snap",
        "channel": 0,
        "rs": str(int(time.time() * 1000)),
        "token": tok,
    }
    url = "https://%s/cgi-bin/api.cgi?%s" % (HOST, urllib.parse.urlencode(params))
    started = time.time()
    with urllib.request.urlopen(url, timeout=30, context=CTX) as response:
        data = response.read()
    if not data.startswith(b"\xff\xd8"):
        raise SystemExit("snap did not return a JPEG: %s" % data[:200])
    with open(outfile, "wb") as handle:
        handle.write(data)
    return {"file": outfile, "bytes": len(data), "seconds": round(time.time() - started, 3)}


def stats(image):
    """Return brightness / clip240 / clip250 / dark for a PIL grayscale image."""
    histogram = image.histogram()
    total = max(sum(histogram), 1)
    return {
        "brightness": round(
            sum(v * c for v, c in enumerate(histogram)) / (255 * total), 4
        ),
        "clip240": round(sum(histogram[240:]) / total, 4),
        "clip250": round(sum(histogram[250:]) / total, 4),
        "dark": round(sum(histogram[:33]) / total, 4),
    }


def measure(path):
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
        full = image.convert("L")
    # Whole frame, matching the controller's thumbnail-then-measure path.
    thumb = full.copy()
    thumb.thumbnail((640, 640), Image.Resampling.BILINEAR)
    result = {"file": os.path.basename(path), "width": width, "height": height}
    result["frame"] = stats(thumb)
    x, y, w, h = PLATE_REGION
    box = (
        int(x * width),
        int(y * height),
        int(min(1.0, x + w) * width),
        int(min(1.0, y + h) * height),
    )
    band = full.crop(box)
    result["plate_band"] = stats(band)
    # Left third: the IR bounce off the near gate post.
    left = full.crop((0, 0, width // 3, height))
    result["left_third"] = stats(left)
    return result


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    mode = sys.argv[1]
    if mode == "get":
        body = [{"cmd": name, "action": 1, "param": {"channel": 0}} for name in sys.argv[2:]]
        print(json.dumps(call(body), indent=1, sort_keys=True))
    elif mode == "raw":
        print(json.dumps(call(json.loads(sys.argv[2])), indent=1, sort_keys=True))
    elif mode == "snap":
        print(json.dumps(snap(sys.argv[2])))
    elif mode == "measure":
        print(json.dumps([measure(p) for p in sys.argv[2:]], indent=1))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
