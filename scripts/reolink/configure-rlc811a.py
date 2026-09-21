#!/usr/bin/env python3
"""Bring a replacement RLC-811A (192.168.0.54) to the gate pipeline's settings.

Runs on the Pi as root. Every block is read first (backed up to a timestamped
directory under /root), the planned change is printed as a field-level diff, and
nothing is written unless --apply is given. After each write the block is read
back and the intended fields asserted.

  sudo python3 /root/configure-rlc811a.py            # dry run: diffs only
  sudo python3 /root/configure-rlc811a.py --apply    # write + verify

It can also be rehearsed away from the camera, against saved GetX blocks, which
is how the test suite pins the values below:

  python3 configure-rlc811a.py --from-capture DIR --plan-json plan.json

The values this script writes are the *deliberate* configuration recorded in
docs/reolink-rlc-811a.md and docs/reviews/2026-09-16-rlc-811a-first-week.md, not
the values the RLC-811A shipped with. Several of them were changed after the
2026-09-11 cutover because the cutover settings were measurably wrong:
`Isp.constantFrameRate` 2 made the encoder drop frames, and 10 fps at a fixed
6144 kbit/s spent the bitrate on frames nobody read. Re-check this file against
those documents before running it on a new unit, and do not "restore" the
cutover-day numbers.

Two things this script deliberately does **not** touch:

* **The clock.** `gate-camera-control` reconciles the camera clock to UTC every
  hour (docs/camera-control.md, Camera clock reconcile). This script only turns
  NTP on; it never sends `SetTime` and never touches the DST block, because
  `SetTime` takes the *displayed* time and the firmware shifts it by the DST
  hour, which is how the camera ended up two hours out for four days.
* **Zoom and focus.** The lens position is being re-derived from a physical
  re-aim (docs/reolink-rlc-811a.md, Capture At The Stop). A hard-coded position
  here would undo that silently, so no `ZoomFocus` command is ever sent.

Sources of truth: docs/reolink-rlc-810a.md (streams, FTP, webhook),
docs/reolink-rlc-811a.md (frame rate, exposure, spotlight),
docs/reviews/2026-09-16-rlc-811a-first-week.md (what the swap did not carry
over), and the old camera's saved blocks under /root/camera-*-before-*.json.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error

sys.path[:0] = ["/root", os.path.dirname(os.path.abspath(__file__))]
import camtool  # noqa: E402  (login/token cache, credentials never printed)

PI_LAN = "192.168.0.33"
DEFAULT_OLD_FTP_BACKUP = "/root/camera-ftp-before-2026-09-07-picinterval.json"
DEFAULT_CONTROLLER_ENV = "/etc/gate-controller.env"
DEFAULT_FTP_CREDENTIALS = "/root/ftp-user.credentials"
SECRET_KEYS = {"password", "hookUrl", "hookBody", "userName"}

# ---------------------------------------------------------------------------
# The pinned values. tests/test_camera_setup_script.py asserts on these through
# a real dry run, so a future edit here cannot silently regress the camera.
# ---------------------------------------------------------------------------

# 6 fps at 6144 kbit/s is 1024 kbit a frame against 614 at the 10 fps the camera
# was cut over on; the bitrate is CBR so the rate is spent either way. gop 1
# keeps a keyframe every second for the on-demand clear-stream grab.
MAIN_STREAM = {"frameRate": 6, "bitRate": 6144, "gop": 1}

# The one setting the swap did not carry over: at 2 the encoder drops frames on
# a quiet scene, and an empty driveway always is one. Measured 2026-09-17:
# 10 fps configured delivered ~5, 6 fps delivered 2.9; at 1, 6 fps delivered 5.9.
CONSTANT_FRAME_RATE = 1

# Manual 1/250 s at gain 16, colour at night under the PIR floodlight.
ISP = {
    "exposure": "Manual",
    "shutter": {"min": 4, "max": 4},
    "gain": {"min": 16, "max": 16},
    "antiFlicker": "Off",
    "backLight": "Off",
    "hdr": 0,
    "nr3d": 1,
    "dayNight": "Color",
    "constantFrameRate": CONSTANT_FRAME_RATE,
}

# NTP on is half the clock fix; the other half was the Hikvision NVR's time push
# to ONVIF channel 5, turned off on the NVR. interval is minutes.
NTP = {"enable": 1, "server": "pool.ntp.org", "port": 123, "interval": 60}

# The Pi's decoder is told the source rate, and a stated rate that is not the
# real one silently changes how many pictures it keeps.
SOURCE_FPS_KEY = "GATE_CLEAR_STREAM_SOURCE_FPS"

VEHICLE_SENSITIVITY = 80
PUSH_INTERVAL_SECONDS = 20
FTP_PIC_INTERVAL_SECONDS = 5

# Rules whose 168-hour schedule this script turns on. Everything else is turned
# off, except a line-crossing row that is already enabled: this firmware
# (v3.1.0.4695) exposes no AI_CROSSLINE_* row at all, but a later one might, and
# zeroing a rule the operator drew by hand is not this script's business.
SCHEDULE_ON = ("AI_VEHICLE",)
SCHEDULE_KEEP_PREFIX = "AI_CROSSLINE"

# Commands this script must never send, whatever a later edit asks for.
FORBIDDEN_COMMANDS = ("SetTime", "SetZoomFocus", "StartZoomFocus", "SetAutoFocus")


def redact(obj):
    """Return `obj` with credentials masked and 168-hour tables summarised."""
    if isinstance(obj, dict):
        return {k: ("***" if k in SECRET_KEYS and obj[k] else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and len(obj) >= 100 and set(obj) <= {"0", "1"}:
        return "<%d-char table, ones=%d>" % (len(obj), obj.count("1"))
    return obj


def diff(before, after, prefix=""):
    """Field-level diff of two blocks, with credential values masked."""
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


def default_check(desired, got):
    return diff(desired, got)


def all_table(value, length=168):
    return str(value) * length


def schedule_for(rule, current):
    """The 168-hour row this script wants for `rule`, given what is there now."""
    if rule in SCHEDULE_ON:
        return all_table(1)
    if rule.startswith(SCHEDULE_KEEP_PREFIX) and "1" in str(current):
        return current
    return all_table(0)


class Camera:
    """The camera, or a directory of saved GetX blocks standing in for one.

    `capture` makes every read come from `<dir>/<Cmd>.json` and every write a
    hard error, so a rehearsal cannot reach a real camera by accident.
    """

    def __init__(self, capture=None):
        self.capture = capture

    def call(self, body, tries=4):
        if self.capture is not None:
            raise AssertionError("no camera call is possible in a --from-capture run")
        for attempt in range(tries):
            try:
                return camtool.call(body)
            except urllib.error.HTTPError as err:
                if err.code == 502 and attempt < tries - 1:
                    print("  camera answered 502 (login throttle); waiting 20 s")
                    time.sleep(20)
                    continue
                raise

    def get(self, cmd, param=None):
        if self.capture is not None:
            return self._from_capture(cmd)
        body = [{"cmd": cmd, "action": 1, "param": param if param is not None else {"channel": 0}}]
        entry = self.call(body)[0]
        if entry.get("code") != 0:
            raise SystemExit("%s failed: %s" % (cmd, json.dumps(entry.get("error"))))
        return entry

    def set(self, cmd, param):
        if cmd in FORBIDDEN_COMMANDS:
            raise AssertionError("%s must never be sent by this script" % cmd)
        entry = self.call([{"cmd": cmd, "action": 0, "param": param}])[0]
        if entry.get("code") != 0:
            raise SystemExit("%s failed: %s" % (cmd, json.dumps(entry.get("error"))))
        return entry

    def _from_capture(self, cmd):
        path = os.path.join(self.capture, cmd + ".json")
        if not os.path.exists(path):
            raise SystemExit("capture has no %s" % os.path.basename(path))
        with open(path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        entries = saved if isinstance(saved, list) else [saved]
        for entry in entries:
            if entry.get("cmd") == cmd:
                return entry
        raise SystemExit("%s holds no %s answer" % (path, cmd))


class Session:
    """One run: what it intends to write, and whether it is allowed to."""

    def __init__(self, camera, apply_changes, backup_dir):
        self.camera = camera
        self.apply = apply_changes
        self.backup_dir = backup_dir
        self.plan = []
        self.warnings = []

    def warn(self, message):
        self.warnings.append(message)
        print("  WARNING: %s" % message)

    def backup(self, name, entry):
        if not self.backup_dir:
            return
        os.makedirs(self.backup_dir, mode=0o700, exist_ok=True)
        path = os.path.join(self.backup_dir, name + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(entry, handle, indent=1)
        os.chmod(path, 0o600)

    def write(self, cmd, param):
        """Record an intended write, and perform it when --apply was given."""
        if cmd in FORBIDDEN_COMMANDS:
            raise AssertionError("%s must never be sent by this script" % cmd)
        self.plan.append({"cmd": cmd, "param": redact(param)})
        if self.apply:
            self.camera.set(cmd, param)

    def block(self, title, cmd_get, cmd_set, key, desired, get_param=None, check=None):
        """Read block, show diff to `desired`, write if --apply, read back and verify."""
        print("\n== %s" % title)
        entry = self.camera.get(cmd_get, get_param)
        self.backup("before-" + cmd_get, entry)
        current = entry["value"][key]
        if "range" in entry:
            self.backup("range-" + cmd_get, entry["range"])
        changes = diff(current, desired)
        if not changes:
            print("  already as intended")
            return current
        print("\n".join(changes))
        self.write(cmd_set, {key: desired})
        if not self.apply:
            return current
        time.sleep(1.5)
        after = self.camera.get(cmd_get, get_param)
        self.backup("after-" + cmd_get, after)
        got = after["value"][key]
        problems = (check or default_check)(desired, got)
        if problems:
            raise SystemExit("VERIFY FAILED for %s: %s" % (cmd_set, "; ".join(problems)))
        print("  written and verified")
        return got


# ----------------------------------------------------------------- the blocks


def configure_streams(session):
    """Clear stream 4K H.265 at the pinned rate, gop 1, audio on."""
    enc = session.camera.get("GetEnc")
    want = json.loads(json.dumps(enc["value"]["Enc"]))
    want["mainStream"].update(MAIN_STREAM)
    want["audio"] = 1  # audio in the streams (recording authorised 2026-09-07)
    session.block(
        "Streams: clear 4K H.265 %d fps %d kbit/s I-frame 1x, audio on"
        % (MAIN_STREAM["frameRate"], MAIN_STREAM["bitRate"]),
        "GetEnc", "SetEnc", "Enc", want,
    )


def configure_isp(session):
    """Manual 1/250 s at gain 16, colour night mode, constant frame rate."""
    isp = session.camera.get("GetIsp")
    want = json.loads(json.dumps(isp["value"]["Isp"]))
    want.update(ISP)
    session.block(
        "ISP: manual 1/250 s gain 16, colour night mode, constantFrameRate %d"
        % CONSTANT_FRAME_RATE,
        "GetIsp", "SetIsp", "Isp", want,
    )


def configure_lights(session):
    """IR off and the camera spotlight off: the PIR floodlight is the only light."""
    session.block("IR illuminator off", "GetIrLights", "SetIrLights", "IrLights", {"state": "Off"})
    wl = session.camera.get("GetWhiteLed")
    want = json.loads(json.dumps(wl["value"]["WhiteLed"]))
    want.update({"state": 0, "mode": 0})
    session.block(
        "Spotlight off (external floodlight is the only plate light)",
        "GetWhiteLed", "SetWhiteLed", "WhiteLed", want,
    )


def configure_clock(session):
    """Turn NTP on and report the clock; never set the time by hand.

    The camera was two hours out for four days after the cutover because NTP was
    off and something on the LAN kept pushing it a local-time block with DST on.
    NTP on is the fix that belongs to the camera. Correcting the displayed time
    belongs to gate-camera-control's hourly reconcile, which owns SetTime; a
    SetTime from here would fight it and, because SetTime carries the displayed
    time, can re-introduce the DST hour.
    """
    print("\n== Clock: NTP on, time left to gate-camera-control")
    ntp = session.camera.get("GetNtp", {})
    current = ntp["value"]["Ntp"]
    want = json.loads(json.dumps(current))
    want.update(NTP)
    changes = diff(current, want)
    if changes:
        print("\n".join(changes))
        session.write("SetNtp", {"Ntp": want})
        if session.apply:
            time.sleep(1.5)
            after = session.camera.get("GetNtp", {})["value"]["Ntp"]
            if after.get("enable") != 1:
                raise SystemExit("VERIFY FAILED for SetNtp: NTP is still disabled")
            print("  written and verified")
    else:
        print("  already as intended")

    time_block = session.camera.get("GetTime", {})["value"]
    session.backup("before-GetTime", time_block)
    zone = time_block.get("Time", {}).get("timeZone")
    dst = time_block.get("Dst", {}).get("enable")
    print("  GetTime: timeZone=%s Dst.enable=%s (want 0 / 0, i.e. the camera displays UTC)" % (zone, dst))
    if zone != 0 or dst != 0:
        session.warn(
            "camera is not on timeZone 0 with DST off, so gate-camera-control "
            "will report skew and leave it alone. Fix it in the camera UI "
            "(Date and Time), not from here: SetTime takes the displayed time "
            "and the firmware adds the DST hour on top."
        )


def configure_ftp(session, credentials_path, old_backup_path):
    """FTP stills into the Pi's watched uploads tree on vehicle alarms."""
    if not os.path.exists(credentials_path):
        print("\n== FTP: SKIPPED, %s is missing. Run: sudo bash /root/reset-ftp-user-password.sh,"
              " then re-run this script." % credentials_path)
        return
    # The camera masks userName/password in every GetFtpV20 answer, so the old
    # camera's backup cannot supply them; they come from the credentials file.
    with open(old_backup_path, "r", encoding="utf-8") as handle:
        old = json.load(handle)
    old = old[0] if isinstance(old, list) else old
    old_ftp = old["value"]["Ftp"]
    with open(credentials_path, "r", encoding="utf-8") as handle:
        cred_text = handle.read()
    ftp_user = re.search(r"^FTP_USER=(.+)$", cred_text, re.M).group(1).strip()
    ftp_password = re.search(r"^FTP_PASSWORD=(.+)$", cred_text, re.M).group(1).strip()
    ftp = session.camera.get("GetFtpV20")
    want = json.loads(json.dumps(ftp["value"]["Ftp"]))
    for field in ("port", "anonymous", "mode", "onlyFtps", "streamType", "remoteDir", "autoDir",
                  "picCaptureMode", "picWidth", "picHeight", "interval", "maxSize", "bpicSingle",
                  "bvideoSingle", "picName", "videoName"):
        if field in want and field in old_ftp:
            want[field] = old_ftp[field]
    want.update({"server": PI_LAN, "userName": ftp_user, "password": ftp_password, "enable": 1})
    want["picInterval"] = FTP_PIC_INTERVAL_SECONDS  # set 2026-09-07, after the old backup
    for rule, current in list(want["schedule"]["table"].items()):
        want["schedule"]["table"][rule] = schedule_for(rule, current)

    def ftp_check(desired, got):
        d = dict(desired)
        g = dict(got)
        for key in ("password", "userName"):  # the camera echoes both masked
            d.pop(key, None)
            g.pop(key, None)
        return diff(d, g)

    session.block(
        "FTP stills to the Pi on vehicle alarms, %d s interval" % FTP_PIC_INTERVAL_SECONDS,
        "GetFtpV20", "SetFtpV20", "Ftp", want, check=ftp_check,
    )
    if session.apply:
        test = session.camera.call([{"cmd": "TestFtp", "action": 0, "param": {"Ftp": want}}])[0]
        print("  TestFtp ->", json.dumps(test.get("error", {"code": test.get("code")})),
              "(a test file should appear under /var/lib/gate-controller/uploads)")
        if test.get("code") != 0:
            raise SystemExit("TestFtp failed: the camera cannot upload to the Pi")


def configure_push(session):
    """Push drives the webhook, so it carries the same vehicle-only schedule."""
    push = session.camera.get("GetPushV20")
    want = json.loads(json.dumps(push["value"]["Push"]))
    want["enable"] = 1
    for rule, current in list(want["schedule"]["table"].items()):
        want["schedule"]["table"][rule] = schedule_for(rule, current)
    session.block("Push (drives the webhook) on vehicle alarms only",
                  "GetPushV20", "SetPushV20", "Push", want)
    session.block("Push interval %d s (firmware minimum)" % PUSH_INTERVAL_SECONDS,
                  "GetPushCfg", "SetPushCfg", "PushCfg",
                  {"pushInterval": PUSH_INTERVAL_SECONDS}, get_param={})


def configure_webhook(session, controller_env_path):
    """Webhook slot 0 at the controller's listener, default body."""
    print("\n== Webhook slot 0 -> http://%s:8766/reolink/events?secret=*** (Content: Default)" % PI_LAN)
    if not os.path.exists(controller_env_path):
        raise SystemExit("webhook needs %s for GATE_REOLINK_WEBHOOK_SECRET" % controller_env_path)
    with open(controller_env_path, "r", encoding="utf-8") as handle:
        env_text = handle.read()
    match = re.search(r"^GATE_REOLINK_WEBHOOK_SECRET=(.+)$", env_text, re.M)
    if not match:
        raise SystemExit("%s holds no GATE_REOLINK_WEBHOOK_SECRET" % controller_env_path)
    secret = match.group(1).strip()
    if not re.fullmatch(r"[A-Za-z0-9]{20,128}", secret):
        raise SystemExit("webhook secret must be 20-128 letters/digits")
    hook_url = "http://%s:8766/reolink/events?secret=%s" % (PI_LAN, secret)
    want = {"channel": 0, "index": 0, "indexEnable": 1, "hookUrl": hook_url, "bCustom": 0, "hookBody": ""}

    def matches(slot):
        # bCustom and hookBody decide the body the controller has to parse, so
        # they are part of "already as intended" and part of the read-back.
        return (slot.get("indexEnable") == 1 and slot.get("hookUrl") == hook_url
                and slot.get("bCustom") == 0 and not slot.get("hookBody"))

    hooks = session.camera.get("GetWebHook")
    session.backup("before-GetWebHook", hooks)
    slot0 = next(h for h in hooks["value"]["WebHook"] if h["index"] == 0)
    if matches(slot0):
        print("  already as intended")
        return
    print("  indexEnable: %s -> 1, hookUrl: %s -> ***, bCustom: %s -> 0, hookBody: %s -> \"\""
          % (slot0.get("indexEnable"), "***" if slot0.get("hookUrl") else '""',
             slot0.get("bCustom"), "***" if slot0.get("hookBody") else '""'))
    session.write("SetWebHook", {"WebHook": want})
    if not session.apply:
        return
    time.sleep(1.5)
    after = session.camera.get("GetWebHook")
    session.backup("after-GetWebHook", after)
    slot0 = next(h for h in after["value"]["WebHook"] if h["index"] == 0)
    if not matches(slot0):
        raise SystemExit("VERIFY FAILED for SetWebHook: %s" % json.dumps(redact(slot0)))
    print("  written and verified")
    # This firmware answers -100 to the documented {"channel", "index"} shape;
    # sending that shape is what makes the answer worth recording.
    test = session.camera.call([{"cmd": "TestWebHook", "action": 0,
                                 "param": {"channel": 0, "index": 0}}])[0]
    print("  TestWebHook ->", json.dumps(test.get("error", {"code": test.get("code")})),
          "(expect rspCode -100 on v3.1.0.4695; treat the first real passage as the acceptance test)")


def configure_ai(session):
    """Vehicle detection sensitivity, frozen at the 2026-09-06 baseline."""
    param = {"channel": 0, "ai_type": "vehicle"}
    ai = session.camera.get("GetAiAlarm", param)
    want = json.loads(json.dumps(ai["value"]["AiAlarm"]))
    want["sensitivity"] = VEHICLE_SENSITIVITY
    session.block("Vehicle AI sensitivity %d" % VEHICLE_SENSITIVITY,
                  "GetAiAlarm", "SetAiAlarm", "AiAlarm", want, get_param=param)


def check_source_fps(session, controller_env_path):
    """The Pi's stated source rate must move with the camera's frame rate."""
    print("\n== %s must match the camera" % SOURCE_FPS_KEY)
    if not os.path.exists(controller_env_path):
        session.warn("%s is missing; check %s by hand" % (controller_env_path, SOURCE_FPS_KEY))
        return
    with open(controller_env_path, "r", encoding="utf-8") as handle:
        match = re.search(r"^%s=(\S+)$" % SOURCE_FPS_KEY, handle.read(), re.M)
    stated = match.group(1).strip() if match else None
    want = str(MAIN_STREAM["frameRate"])
    if stated == want:
        print("  %s=%s, matching the camera" % (SOURCE_FPS_KEY, stated))
        return
    session.warn(
        "%s is %s but the camera is being set to %s fps. The session decoder "
        "reads a pipe with no timestamps, so a stated rate that is not the real "
        "one silently changes how many pictures it keeps. Set %s=%s in %s and "
        "restart file-monitor.service."
        % (SOURCE_FPS_KEY, stated or "unset", want, SOURCE_FPS_KEY, want, controller_env_path)
    )


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Configure the gate RLC-811A. Reads everything, writes only with --apply.",
        epilog="This script takes no credentials: the camera password comes from "
               "/etc/gate-media-gateway.env via camtool, the FTP password from the "
               "credentials file, and the webhook secret from the controller env file. "
               "Never pass a password on the command line; it would be visible in ps.",
    )
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--from-capture", metavar="DIR",
                        help="read saved <Cmd>.json blocks instead of the camera; forbids --apply")
    parser.add_argument("--plan-json", metavar="PATH",
                        help="write the redacted list of intended writes to PATH")
    parser.add_argument("--controller-env", default=DEFAULT_CONTROLLER_ENV)
    parser.add_argument("--ftp-credentials", default=DEFAULT_FTP_CREDENTIALS)
    parser.add_argument("--old-ftp-backup", default=DEFAULT_OLD_FTP_BACKUP)
    parser.add_argument("--backup-dir", help="where to save the before/after blocks")
    args = parser.parse_args(argv)
    if args.apply and args.from_capture:
        parser.error("--apply cannot be combined with --from-capture")
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    backup_dir = args.backup_dir
    if backup_dir is None and args.from_capture is None:
        backup_dir = "/root/rlc811a-swap-%s" % time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    session = Session(Camera(args.from_capture), args.apply, backup_dir)

    configure_streams(session)
    configure_isp(session)
    configure_lights(session)
    configure_clock(session)
    configure_ftp(session, args.ftp_credentials, args.old_ftp_backup)
    configure_push(session)
    configure_webhook(session, args.controller_env)
    configure_ai(session)
    check_source_fps(session, args.controller_env)

    if args.plan_json:
        with open(args.plan_json, "w", encoding="utf-8") as handle:
            json.dump(session.plan, handle, indent=1, sort_keys=True)

    print("\nBackups: %s" % (backup_dir if backup_dir and os.path.isdir(backup_dir) else "(none written)"))
    print("Intended writes: %s" % (", ".join(step["cmd"] for step in session.plan) or "(none)"))
    if session.warnings:
        print("Warnings: %d — read them above before commissioning." % len(session.warnings))
    if not args.apply:
        print("DRY RUN: nothing was written. Re-run with --apply.")
    else:
        print("Left alone on purpose: SD recording schedule, email (no address), OSD, "
              "AutoUpgrade=1, PowerLed=On, the camera clock (gate-camera-control owns it), "
              "and zoom/focus (set at the gate after the re-aim, never from here).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
