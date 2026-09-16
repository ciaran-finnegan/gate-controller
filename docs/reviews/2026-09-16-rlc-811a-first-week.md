# RLC-811A first week: why nothing was recognised, 2026-09-16

The RLC-811A was fitted on the pillar mount on Friday 2026-09-11 at about
20:00 Irish time, replacing the RLC-810A. Between then and this review the
gate opened once. This document records what the Pi journal, the camera API,
the dashboard data and the evidence images show, so the commissioning steps in
[reolink-rlc-811a.md](../reolink-rlc-811a.md) are done in the right order and
the next person does not start from the controller code. The controller did
not change: release `f5c2949` (2026-09-09) was running before and after.

Sources: `journalctl -u file-monitor.service --since 2026-09-12` on the Pi,
`/root/camtool.py get ...` against the camera (read-only), the live
`/etc/gate-controller.env`, and the Gate Mate Worker (`/api/access-logs`,
`/api/access-logs/<id>/reviews`, `/api/access-logs/<id>/image`,
`/api/controller-status`). Event ids are dashboard ids; times are Irish local
unless marked UTC.

## Timeline

| | |
| --- | --- |
| Last grant before the swap | 3715, Fri 19:26, `172L66`, read on the Pi at 1.00, webhook-matched |
| Controller process start | Sat 2026-09-12 13:19 (heartbeat `process_uptime_seconds`) |
| Grants since | one: 3742, Sat 18:29, `172L66`, cloud 0.954, FTP still only |
| Partial reads since | 3724 `11WH257` 0.86, 3731 `172L56` 0.84, 3789 local `131D4041` 0.29 |
| Everything else since | `no_match` with no plate, `decision_timeout`, `ocr_error` |

## 1. Every webhook is rejected as stale: the camera clock is wrong

The webhook **is** configured and firing. `GetWebHook` shows rule 0 enabled,
default body, URL `http://192.168.0.33:8766/reolink/events?secret=...`, and the
listener is bound on the Pi. Since process start the journal holds exactly one
kind of webhook line, 24 times:

```
reolink_webhook status=rejected reason=stale
```

and no `gate_trigger_capture` line at all. `stale` means the camera's
`alarmTime` is more than 15 s from Pi time. Measured at 2026-09-16 02:17 UTC:

| Clock | Reads |
| --- | --- |
| Pi (`date -u`) | 02:17:18 UTC, 03:17 Irish |
| Camera `GetTime` | 04:17:17, `timeZone 0`, `Dst.enable 1`, `isDst 1` |
| Camera `GetNtp` | `enable 0` |

The camera's clock is set by hand, NTP is off, and the alarm timestamp it
posts (with a `+0000` suffix) lands about two hours in the future. Every
rejected webhook means: no clear-stream capture series at the stop, no presence
retries, no correlation, so every event is `camera_ftp / unverified` and only
the one moving-vehicle FTP still is ever read. Before the swap almost every
event was `reolink_webhook / vehicle / vehicle_detected_from_front_gate /
matched` and most grants were webhook captures read on the Pi.

The rest of the capture path is healthy and needs no change: MediaMTX serves
`clear` (H.265 3840x2160 10 fps, `gop 1`) and `camera` (H.264 640x360 10 fps),
`GATE_TRIGGER_CAPTURE_ENABLED=true` with source `rtsp://127.0.0.1:8554/clear`,
delay 2.0 s, count 3, spacing 1.5 s, `drm` hwaccel, and the local reader logs
`gate_local_ocr stage=active` on every frame with no `unavailable` line.

**Fix, applied 2026-09-16 02:45 UTC.** The web UI's Network > NTP Settings
dialog saves only the server and port and reports success without enabling
synchronisation; the enable flag is separate. It was set from the Pi with

```sh
sudo /root/camtool.py raw '[{"cmd":"SetNtp","action":0,"param":{"Ntp":{"enable":1,"server":"pool.ntp.org","port":123,"interval":1440}}}]'
```

and within 20 s `GetTime` read 03:45:33 (`timeZone 0`, DST) against the Pi's
02:45:33 UTC, with no camera restart. Keep `timeZone 0` with DST for Ireland.
The next real arrival must produce `gate_trigger_capture outcome=scheduled`
and then `outcome=captured`; `TestWebHook` over the API returns `param error`
for every payload shape tried, so use the Test button in the camera UI if a
synthetic check is wanted. Do not widen the 15 s staleness window in the
controller to paper over a clock; a wrong camera clock also mis-stamps the FTP
overlay and the corpus.

**Reverted and re-fixed the same day.** At 11:44 UTC `GetNtp` read
`enable 0` again and the display was back to UTC+2 (13:44 against 11:44 UTC);
six more webhooks between 09:22 and 11:07 UTC were rejected `stale`. The
owner had been saving pages in the camera web UI in between. Two UI traps
account for it: the Network > NTP Settings dialog submits the `Ntp` block
with `enable 0` because it has no enable toggle, and the Date and Time page
re-submits the displayed local time as the base clock, which adds the DST
hour on every save. Re-enabling NTP alone did not re-sync within two minutes
the second time, so the clock was set explicitly with

```sh
sudo /root/camtool.py raw '[{"cmd":"SetTime","action":0,"param":{"Time":{<GetTime.value.Time with year/mon/day/hour/min/sec = UTC now, isDst removed>},"Dst":{<GetTime.value.Dst unchanged>}}}]'
```

followed by `SetNtp enable 1`; the display then read UTC+1 as it should. The
correct display for `timeZone 0` with DST active is UTC+60 min; anything
else means the base clock is wrong. Until the swap is commissioned, do not
save the camera's NTP or Date and Time pages from the web UI or app; re-check
`GetNtp` and `GetTime` after any camera UI session.

### The clock keeps reverting: measured behaviour, 2026-09-16 11:45 to 12:30 UTC

Repeated fixes were overwritten within 20 s to 5 min, always to the same
state: `Dst.enable 1` (Irish rules), `Ntp.enable 0`, display UTC+2. Facts
established by experiment from the Pi:

- Nothing on the Pi issues `SetTime`/`SetNtp`; its only camera sessions are
  the two MediaMTX RTSP pulls. `GetOnline` listed only `admin@192.168.0.33`
  at every reversion.
- With NTP off, a `SetTime` holds for as long as watched until the next
  external overwrite.
- `SetTime.Time` is the **displayed** time. Changing `Dst.enable` in the same
  call shifts the display by the DST hour: sending Irish local time (UTC+1)
  while turning DST on yields UTC+2. That is the +2 h signature.
- Enabling NTP, by hostname or by `162.159.200.123`, never corrected the
  clock; the flag itself was overwritten by the same external write.
- A camera reboot came back in the overwritten state.

The writer is therefore a client that pushes a full date/time block
(time from its own clock, DST rules, NTP off) at intervals, the way the
Reolink app's and Client's "synchronise with phone/PC time" does, and the
firmware applies the DST hour on top. Reolink documents both halves:
[The Synchronization Time Difference is One Hour](https://support.reolink.com/hc/en-us/articles/900002179266-The-Synchronization-Time-Difference-is-One-Hour-Summer-Time-and-Winter-Time/),
[Strange time sync behavior](https://community.reolink.com/topic/2702/strange-time-sync-behavior)
(an NVR overwriting camera time), and
[Time Update Issue - Reverts back after syncing](https://community.reolink.com/topic/1051/time-update-issue-reverts-back-after-syncing-to-new-time-for-all-3-x-cameras).
A ten-minute test with P2P disabled (`SetP2p enable 0`, re-enabled
automatically) did not stop it: the overwrite landed 30 s after P2P went
off, while the P2P setting itself stayed off for the whole window. So the
writer is on the LAN, it reaches the camera without an HTTP API login
(Reolink's port 9000 protocol or ONVIF on port 8000), and it rewrites only
the time block. A LAN sweep from the Pi found the household's other
cameras are Hikvision (`DS-2CD2T42WD` at 192.168.0.5 and .8, an
`IP Camera(F1905)` at .9, a fourth digest-auth camera at .2) and two hosts
at 192.168.0.3 and .41 serving a Hikvision-style NVR web UI. A Hikvision
NVR that has the Reolink added as an ONVIF channel syncs its own time to
that channel periodically, which matches every observation; check the NVR's
channel list first. **Confirmed and fixed 2026-09-16 12:55 UTC.** The owner identified the
Hikvision DS-7608NI-K2 NVR at 192.168.0.2 (firmware V4.74.200), which records
the gate camera as ONVIF channel 5. Its channel list at
`/ISAPI/ContentMgmt/InputProxy/channels` carries `<enableTiming>true</enableTiming>`
per channel, which is the NVR's periodic time push to that camera. It was
turned off for channel 5 only, with a `PUT` of the channel XML to
`/ISAPI/ContentMgmt/InputProxy/channels/5` carrying
`<enableTiming>false</enableTiming>`, from a browser session the owner had
logged in (the Hikvision web UI holds the session cookie; the ISAPI calls
were made with `fetch` from that page). The Hikvision channels 1, 3 and 4
keep their sync. The NVR runs `timeMode NTP` with time zone
`CST+0:00:00DST01:00:00,M3.5.0/00:00:00,M10.5.0/00:00:00`, i.e. UTC with
Irish DST, so it was pushing Irish local time; the Reolink then added the DST
hour again. The camera was then left on `timeZone 0`, DST off, NTP on
(`pool.ntp.org`, 60 min), displaying UTC. Reaching the NVR from the Mac:
`ssh -i ~/.ssh/gate_pi_claude -o IdentitiesOnly=yes -N -L 8083:192.168.0.2:80 pi@100.90.85.12`
then `http://localhost:8083`.

Whatever the writer, the controller should not depend on the camera clock
for the trigger path: the receipt-time freshness check and the event-id
de-duplication already bound replay, so `alarmTime` skew can be recorded as
telemetry and warned about instead of rejecting the event. Separately, the
camera-control service can own the camera clock with an hourly
read-back-and-correct using `GetTime`/`SetTime` (display semantics above).

## 2. Lens at the wide end, aimed too low, plate above the OCR crop

`GetZoomFocus` reads `zoom.pos 2`, `focus.pos 78`: the motorised lens is at
the wide end (105 degrees), wider than the RLC-810A's 87 degrees. The evidence
frames show the fence line along the top edge and gravel over roughly 70% of
the picture; the RLC-810A frames had the tree line in the upper third.

| Event | When | Plate centre, fraction of frame height | Distance and angle | Read |
| --- | --- | --- | --- | --- |
| 3714, 3703 (RLC-810A) | Fri 18:26, 17:04 | 0.65 to 0.80 | 4 to 5 m, near head-on | 1.00 local |
| 3742 | Sat 18:29 | 0.26 | 2 to 3 m, about 40 degrees | cloud 0.95 |
| 3791, 3731 | Tue 17:13, Sat 14:48 | 0.25 | 2 to 3 m, oblique | none / misread |
| 3792 | Tue 17:52 | 0.06, rear plate, motion blur | leaving | none |
| 3762 | Mon 07:56 | vehicle half out of the left edge | | none |

The live band is `GATE_PLATE_REGION=0,0.26,1,0.60`, and the journal confirms
the upload crop on every frame:

```
gate_ocr upload_downscale=applied source_width=3840 upload_width=1920 crop=0,560,3840,1856
```

That keeps rows 560 to 1856 of 2160, i.e. 26% to 86% of the frame height. With
the plate centred at 25% to 30% it straddles the top edge of the crop, and a
rear plate at 6% is outside it entirely. That is why the local reader, which
read the old framing at 0.99 to 1.00, returns `no_plate` on every frame, and
why the cloud read only the frames where a car crept close enough to drop the
plate into the band. There is no `gate_ocr plate_box=` line since the swap.
Plate width in the 4K frame is about 280 to 310 px, so size is not the
problem; height in frame and angle are.

**Fix:** aim up the drive at the approach, not across it at the stop. Site
photos on 2026-09-16 show the mount is on the fence about 1 m before the gate,
1.5 to 2 m to the right of the drive centreline, so a car stopped 1 to 3 m
from the gate presents its plate at 40 to 90 degrees; the geometry table and
aiming steps are now in [reolink-rlc-811a.md](../reolink-rlc-811a.md#capture-at-the-stop).
Target: a car 5 m from the gate mid-frame, plate at mid-height, tree line in
the top third, matching event 3714. Zoom (camera UI only; the Pi has no zoom
command) only until the drive at the 5 m point fills the frame width.

**Interim, applied 2026-09-16 03:56 UTC while the aim is unchanged:**
`GATE_PLATE_REGION=0.25,0,0.75,0.6` and
`GATE_TRIGGER_CAPTURE_DELAY_SECONDS=0`, controller restarted, previous file
kept at `/etc/gate-controller.env.bak-2026-09-16`. Every post-swap plate sat
in the top 30% of the frame and right of x=0.3, so this band contains it, and
because the upload is capped at 1920 px wide the narrower crop leaves the
plate about 190 px wide instead of 140. Delay 0 grabs the clear-stream frames
before the car rolls past the readable point. After re-aiming, unset the band
for a day, collect `gate_ocr plate_box=` lines, then set a band that contains
them with margin.

### Result of the interim change, first eight hours

Eight events between 09:22 and 11:07 UTC on 2026-09-16 (dashboard 3793 to
3800). Both arrivals (3795, 3799, `131D2696`) were read on the Pi at 0.999
and opened the gate; the FTP still showed the plate at about 0.33 of frame
height, inside the new band. All six denials are the same vehicle leaving:
rear plate at 0.10 to 0.15 of frame height, motion-blurred, local `no_plate`,
then one cloud lookup each (`recognition_lookups_month_to_date` 197 to 203).
Stage timing on the two grants: image arrival to burst 1.47 and 1.60 s, burst
to OCR 82 and 88 ms, OCR (local) 419 and 447 ms, decision to relay 160 ms,
filesystem ingress to decision 1.97 and 2.13 s. The heartbeat still reports
`trigger_capture.captures 0` since the 03:56 UTC restart, so no webhook was
accepted in that window either; the journal reason is still to be read.

## 3. Detection rules: AI vehicle only, no line-crossing rule, zone undrawn

`GetFtpV20` and `GetPushV20` both carry only `AI_VEHICLE` at 168/168; this
firmware (`v3.1.0.4695_2504301440`) exposes no `AI_CROSSLINE_*` rows, so the
"line 1 m before the plate mark" from the swap document does not exist on this
unit. Both trigger paths fire on generic vehicle detection anywhere in the
zone, which is why the FTP still catches the car 2 to 3 m out and mid-turn.
`GetAiCfg` also has `aiTrack 1` and `trackType.people 1`; check in the camera
UI that no auto-tracking or auto-zoom feature can move the framing.
Event 3762 fired with the car half outside the left edge, so the detection
zone needs redrawing over the drive 3 to 8 m out from the gate once the aim
is set.

## 4. Night settings did carry over

`GetIsp` reads `exposure Manual`, `shutter 4/4` (1/250 s), `gain 16/16`,
`dayNight Color`, `hdr 0`, `antiFlicker Off`, `backLight Off`; `GetIrLights`
is `Off`; `GetWhiteLed` `state 0`. These match the 2026-09-06 RLC-810A
configuration. Event 3759 (Sun 21:00) still shows the plate inside the
headlight blaze with no floodlight lit, so the floodlight coverage and hold
timer remain the open night item, not the ISP.

## 5. Pre-existing: cloud OCR overruns the budget

`GATE_DECISION_TIMEOUT_SECONDS=7` on the Pi. `ocrMs` of 5.2 to 6.7 s appears
on roughly a quarter of events, recorded as `decision_timeout` with an
`ocr_timeout` attempt. This predates the swap, but with the local reader blind
every passage now depends on that one slow call.

## Order of work

1. Done 2026-09-16: NVR time push to channel 5 off, camera on UTC with DST
   off and NTP on, clock stable. Confirm `gate_trigger_capture
   outcome=scheduled` and `outcome=captured` on the next arrival.
2. Done 2026-09-16: interim band `GATE_PLATE_REGION=0.25,0,0.75,0.6` and
   `GATE_TRIGGER_CAPTURE_DELAY_SECONDS=0`; both arrivals since were read on
   the Pi and opened the gate.
3. Re-aim up the drive at the approach (car at 5 m mid-frame, tree line in
   the top third), zoom until the drive at 5 m fills the frame width, and set
   `GATE_TRIGGER_CAPTURE_DELAY_SECONDS=0`.
4. Redraw the vehicle detection zone over the drive 3 to 8 m out from the
   gate, sensitivity 80. If the UI offers a line-crossing rule on this firmware,
   add it as in the swap document; the API tables suggest it does not.
5. Re-derive `GATE_PLATE_REGION` from `gate_ocr plate_box=` lines over a day.
6. Verify the floodlight covers the stop and holds for 1 to 2 minutes; keep
   IR and the camera spotlight off.

## Controller follow-ups worth doing regardless

- Alert in the dashboard when webhooks are being rejected, and when
  `trigger_capture.captures` has not advanced for hours; this failure was
  silent for four days.
- Record the OCR crop box in event telemetry so the dashboard shows whether the
  plate was inside the upload.
- Age-bound the clear-stream keyframe ring so a stale keyframe cannot match the
  idle baseline and be discarded as an empty scene.
- Publish `ClearStreamSource.status()` in the heartbeat; it is built and never
  called.
