#!/usr/bin/env python3
"""Bring the replacement RLC-811A (192.168.0.54) to the gate pipeline's settings.

Runs on the Pi as root. Every block is read first (backed up to a timestamped
directory under /root), the planned change is printed as a field-level diff, and
nothing is written unless --apply is given. After each write the block is read
back and the intended fields asserted.

  sudo python3 /root/configure-rlc811a.py            # dry run: diffs only
  sudo python3 /root/configure-rlc811a.py --apply    # write + verify

Sources of truth: docs/reolink-rlc-810a.md (streams, FTP, webhook),
docs/reolink-rlc-811a.md (exposure, spotlight), and the old camera's saved blocks
under /root/camera-*-before-*.json.
"""
import json
import os
import re
import sys
import time
import urllib.error

sys.path[:0] = ["/root", os.path.dirname(os.path.abspath(__file__))]
import camtool  # noqa: E402  (login/token cache, credentials never printed)

APPLY = "--apply" in sys.argv
STAMP = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
BACKUP_DIR = "/root/rlc811a-swap-%s" % STAMP
PI_LAN = "192.168.0.33"
OLD_FTP_BACKUP = "/root/camera-ftp-before-2026-09-07-picinterval.json"
CONTROLLER_ENV = "/etc/gate-controller.env"
FTP_CREDENTIALS = "/root/ftp-user.credentials"
SECRET_KEYS = {"password", "hookUrl", "hookBody", "userName"}


def call(body, tries=4):
    for attempt in range(tries):
        try:
            return camtool.call(body)
        except urllib.error.HTTPError as err:
            if err.code == 502 and attempt < tries - 1:
                print("  camera answered 502 (login throttle); waiting 20 s")
                time.sleep(20)
                continue
            raise


def get(cmd, param=None):
    body = [{"cmd": cmd, "action": 1, "param": param if param is not None else {"channel": 0}}]
    entry = call(body)[0]
    if entry.get("code") != 0:
        raise SystemExit("%s failed: %s" % (cmd, json.dumps(entry.get("error"))))
    return entry


def set_(cmd, param):
    entry = call([{"cmd": cmd, "action": 0, "param": param}])[0]
    if entry.get("code") != 0:
        raise SystemExit("%s failed: %s" % (cmd, json.dumps(entry.get("error"))))
    return entry


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("***" if k in SECRET_KEYS and obj[k] else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and len(obj) >= 100 and set(obj) <= {"0", "1"}:
        return "<%d-char table, ones=%d>" % (len(obj), obj.count("1"))
    return obj


def backup(name, entry):
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(BACKUP_DIR, name + ".json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(entry, handle, indent=1)
    os.chmod(path, 0o600)


def diff(before, after, prefix=""):
    lines = []
    keys = sorted(set(before) | set(after))
    for key in keys:
        b, a = before.get(key), after.get(key)
        if isinstance(b, dict) and isinstance(a, dict):
            lines += diff(b, a, prefix + key + ".")
        elif b != a:
            if key in SECRET_KEYS:
                b, a = ("***" if b else b), ("***" if a else a)
            lines.append("  %s%s: %s -> %s" % (prefix, key, json.dumps(redact(b)), json.dumps(redact(a))))
    return lines


def apply_block(title, cmd_get, cmd_set, key, desired, get_param=None, check=None):
    """Read block, show diff to `desired`, write if --apply, read back and verify."""
    print("\n== %s" % title)
    entry = get(cmd_get, get_param)
    backup("before-" + cmd_get, entry)
    current = entry["value"][key]
    if "range" in entry:
        backup("range-" + cmd_get, entry["range"])
    changes = diff(current, desired)
    if not changes:
        print("  already as intended")
        return current
    print("\n".join(changes))
    if not APPLY:
        return current
    set_(cmd_set, {key: desired})
    time.sleep(1.5)
    after = get(cmd_get, get_param)
    backup("after-" + cmd_get, after)
    got = after["value"][key]
    problems = (check or default_check)(desired, got)
    if problems:
        raise SystemExit("VERIFY FAILED for %s: %s" % (cmd_set, "; ".join(problems)))
    print("  written and verified")
    return got


def default_check(desired, got):
    return diff(desired, got)


def all_table(value, length=168):
    return str(value) * length


# ---------------------------------------------------------------- 1. streams
enc = get("GetEnc")
enc_want = json.loads(json.dumps(enc["value"]["Enc"]))
enc_want["mainStream"].update({"frameRate": 10, "gop": 1})   # 10 fps, keyframe every second
enc_want["audio"] = 1                                          # audio in the streams (recording authorised 2026-09-07)
print("Enc range:", json.dumps(redact(enc.get("range", {}).get("Enc")))[:600])
apply_block("Streams: clear 4K H.265 10 fps I-frame 1x, audio on", "GetEnc", "SetEnc", "Enc", enc_want)

# --------------------------------------------------------------------- 2. ISP
isp = get("GetIsp")
isp_want = json.loads(json.dumps(isp["value"]["Isp"]))
isp_want.update({
    "exposure": "Manual",
    "shutter": {"min": 4, "max": 4},      # 1/250 s: freezes the crossing frame
    "gain": {"min": 16, "max": 16},       # exposure budget spent on gain, not shutter
    "antiFlicker": "Off",
    "backLight": "Off",
    "hdr": 0,
    "nr3d": 1,
    "dayNight": "Color",                  # colour night mode under the PIR floodlight (proven 2026-09-08)
})
print("Isp range:", json.dumps({k: isp["range"]["Isp"].get(k) for k in ("shutter", "gain", "constantFrameRate", "dayNight", "exposure")}))
apply_block("ISP: manual 1/250 s gain 16, colour night mode", "GetIsp", "SetIsp", "Isp", isp_want)

# --------------------------------------------------------------- 3. IR lights
apply_block("IR illuminator off", "GetIrLights", "SetIrLights", "IrLights", {"state": "Off"})

# ---------------------------------------------------------------- 4. spotlight
wl = get("GetWhiteLed")
wl_want = json.loads(json.dumps(wl["value"]["WhiteLed"]))
wl_want.update({"state": 0, "mode": 0})   # the PIR floodlight is the only plate light (rlc-811a doc, Night Light)
apply_block("Spotlight off (external floodlight is the only light)", "GetWhiteLed", "SetWhiteLed", "WhiteLed", wl_want)

# --------------------------------------------------------------------- 5. FTP
# The camera masks userName/password in every GetFtpV20 answer, so the old
# camera's backup cannot supply them; they come from /root/ftp-user.credentials.
with open(OLD_FTP_BACKUP, "r", encoding="utf-8") as handle:
    old = json.load(handle)
old = old[0] if isinstance(old, list) else old
old_ftp = old["value"]["Ftp"]
if not os.path.exists(FTP_CREDENTIALS):
    print("\n== FTP: SKIPPED, %s is missing. Run: sudo bash /root/reset-ftp-user-password.sh, then re-run this script." % FTP_CREDENTIALS)
    cred_text = None
else:
    with open(FTP_CREDENTIALS, "r", encoding="utf-8") as handle:
        cred_text = handle.read()
if cred_text:
    ftp_user = re.search(r"^FTP_USER=(.+)$", cred_text, re.M).group(1).strip()
    ftp_password = re.search(r"^FTP_PASSWORD=(.+)$", cred_text, re.M).group(1).strip()
    ftp = get("GetFtpV20")
    ftp_want = json.loads(json.dumps(ftp["value"]["Ftp"]))
    for field in ("port", "anonymous", "mode", "onlyFtps", "streamType", "remoteDir", "autoDir", "picCaptureMode",
                  "picWidth", "picHeight", "interval", "maxSize", "bpicSingle", "bvideoSingle", "picName", "videoName"):
        if field in ftp_want and field in old_ftp:
            ftp_want[field] = old_ftp[field]
    ftp_want.update({"server": PI_LAN, "userName": ftp_user, "password": ftp_password, "enable": 1})
    ftp_want["picInterval"] = 5                      # set 2026-09-07 after the old backup was taken
    for rule in ftp_want["schedule"]["table"]:
        ftp_want["schedule"]["table"][rule] = all_table(1 if rule == "AI_VEHICLE" else 0)


    def ftp_check(desired, got):
        d = dict(desired); g = dict(got)
        for k in ("password", "userName"):            # the camera echoes both masked
            d.pop(k, None); g.pop(k, None)
        return diff(d, g)


    apply_block("FTP stills to the Pi on vehicle alarms, 5 s interval", "GetFtpV20", "SetFtpV20", "Ftp", ftp_want, check=ftp_check)
    if APPLY:
        test = call([{"cmd": "TestFtp", "action": 0, "param": {"Ftp": ftp_want}}])[0]
        print("  TestFtp ->", json.dumps(test.get("error", {"code": test.get("code")})), "(a test file should appear under /var/lib/gate-controller/uploads)")

# -------------------------------------------------------------------- 6. push
push = get("GetPushV20")
push_want = json.loads(json.dumps(push["value"]["Push"]))
push_want["enable"] = 1
for rule in push_want["schedule"]["table"]:
    push_want["schedule"]["table"][rule] = all_table(1 if rule == "AI_VEHICLE" else 0)
apply_block("Push (drives the webhook) on vehicle alarms only", "GetPushV20", "SetPushV20", "Push", push_want)
apply_block("Push interval 20 s (firmware minimum)", "GetPushCfg", "SetPushCfg", "PushCfg", {"pushInterval": 20}, get_param={})

# ----------------------------------------------------------------- 7. webhook
with open(CONTROLLER_ENV, "r", encoding="utf-8") as handle:
    secret = re.search(r"^GATE_REOLINK_WEBHOOK_SECRET=(.+)$", handle.read(), re.M).group(1).strip()
assert re.fullmatch(r"[A-Za-z0-9]{20,128}", secret), "webhook secret must be 20-128 letters/digits"
hook_url = "http://%s:8766/reolink/events?secret=%s" % (PI_LAN, secret)
print("\n== Webhook slot 0 -> http://%s:8766/reolink/events?secret=*** (Content: Default)" % PI_LAN)
hooks = get("GetWebHook")
backup("before-GetWebHook", hooks)
slot0 = next(h for h in hooks["value"]["WebHook"] if h["index"] == 0)
hook_want = {"channel": 0, "index": 0, "indexEnable": 1, "hookUrl": hook_url, "bCustom": 0, "hookBody": ""}
current_ok = slot0.get("indexEnable") == 1 and slot0.get("hookUrl") == hook_url and slot0.get("bCustom") == 0
if current_ok:
    print("  already as intended")
else:
    print("  indexEnable: %s -> 1, hookUrl: %s -> ***, bCustom: %s -> 0" % (slot0.get("indexEnable"), "***" if slot0.get("hookUrl") else '""', slot0.get("bCustom")))
    if APPLY:
        set_("SetWebHook", {"WebHook": hook_want})
        time.sleep(1.5)
        after = get("GetWebHook")
        backup("after-GetWebHook", after)
        slot0 = next(h for h in after["value"]["WebHook"] if h["index"] == 0)
        if not (slot0.get("indexEnable") == 1 and slot0.get("hookUrl") == hook_url):
            raise SystemExit("VERIFY FAILED for SetWebHook: %s" % json.dumps(redact(slot0)))
        print("  written and verified")
        test = call([{"cmd": "TestWebHook", "action": 0, "param": {"WebHook": hook_want}}])[0]
        print("  TestWebHook ->", json.dumps(test.get("error", {"code": test.get("code")})),
              "(expect gate_trigger_capture outcome=skipped_type / manual_test in the controller journal)")

# ------------------------------------------------------- 8. vehicle sensitivity
ai = get("GetAiAlarm", {"channel": 0, "ai_type": "vehicle"})
ai_want = json.loads(json.dumps(ai["value"]["AiAlarm"]))
ai_want["sensitivity"] = 80                         # the frozen baseline from the 2026-09-06 review
apply_block("Vehicle AI sensitivity 80", "GetAiAlarm", "SetAiAlarm", "AiAlarm", ai_want, get_param={"channel": 0, "ai_type": "vehicle"})

# ------------------------------------------------------------------- summary
print("\nBackups: %s" % (BACKUP_DIR if os.path.isdir(BACKUP_DIR) else "(none written)"))
if not APPLY:
    print("DRY RUN: nothing was written. Re-run with --apply.")
else:
    print("Left alone on purpose: SD recording schedule, email (no address), OSD, AutoUpgrade=1, PowerLed=On, zoom/focus (needs a parked car).")
