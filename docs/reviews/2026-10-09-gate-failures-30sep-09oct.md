# Gate failures, 30 September - 9 October 2026

The gate opened automatically for about 55% of passages from 16 to 30
September and for **1 of 34** from 1 October until the camera was refocused
on 5 October. Several separate faults overlapped. This records what each was,
how it was found, what fixed it, and what is still open. The invariants it
led to are in [../invariants.md](../invariants.md).

## What happened

| When | Fault | Effect | Fixed by |
| --- | --- | --- | --- |
| 23 Sep - 5 Oct | GitHub never ran CI on #190's merge; the suite had also started failing on the calendar (a thumbnail test aging fixtures by wall clock). | The Pi stayed on the 22 Sep release; no fix could deploy. | #191 |
| 30 Sep 21:52 - 4 Oct ~03:00 | Gate-end network degraded: camera-to-Pi uploads at 3-40 KB/s (normally ~1,300), internet down. | Every still rejected `upload_incomplete` after ~5 s, unread, photo discarded: 24 passages refused, no images for 1-3 Oct. | #192 (wait while an upload is still arriving; keep it; staleness still decides) |
| night of 3-4 Oct | The camera's autofocus settled out of focus (focus 86 on zoom 2) while the network recovered. | Every daytime frame soft (sharpness <= 0.158 against 0.19-0.29), daytime JPEGs ~390 KB against 1.2-1.8 MB, junk reads. | Zoom nudged 2 -> 3 -> 2 on 5 Oct (focus 78); #195 does it daily and on soft frames |
| standing | Far frames sent to the cloud: five hand-overs in the first ~9 s of a sweep, and the camera's alarm still, all with the car far down the lane. | Billed lookups that could not succeed; the cloud lane busy when the useful frame arrived. | #193 (hold until 220 px or stopped; hold the alarm still while a sweep reads) |
| standing | The sweep stopped reading a waiting car at 40 s, read slowest while it waited, and one confident cloud misread (`1SU2U` at 0.811) could end a passage. | Cars still at the gate stopped being read. | #196 |
| 22 Sep - 6 Oct | Fast-lane regression: #183 + #189 let the burst thread post to the cloud when the breaker closed between routing and finishing a burst. | 5 Oct 21:31: an Audi read at 0.999 at +3.6 s, gate opened at +10.0 s. | #197 |

## How it was found

- **Focus**: per-frame `sharpness` in event telemetry fell as a step on 4 Oct;
  the FTP log's daytime file sizes fell the same night, giving the window to
  the hour; a live 4K still showed bokeh on the bright gaps in the trees
  (defocus, not droplets). `GetZoomFocus` showed focus 86 against the 80
  recorded at the 20 Sep re-aim.
- **Network**: vsftpd's `OK UPLOAD` lines with their transfer rates; the
  controller had logged each of those uploads `upload_incomplete`. The Pi's
  audio feed from the camera (`gate_listening`) and pings showed the switch
  inside the gate-end powerline adapter is clean even when the link to the
  house is lossy: the slow uploads came from the camera itself, most likely
  held up by its clients on the house side (NVR, app), not from the wire.
- **The 6.4 s delay**: the journal of 5 Oct 21:31 showed the authorised frame
  queued for 6.2 s (`burst_to_ocr_ms=6242`) while the camera's still, kept on
  the burst thread as unreachable, posted after the breaker closed. Replayed
  through the real code in
  `tests/test_fast_lane.py::test_a_burst_kept_off_the_lane_never_waits_on_the_cloud_when_the_breaker_closes`,
  which fails on the old code.
- **Stuck release**: the updater's own journal ("does not yet have successful
  exact-SHA CI") and the Actions run list, which had no push run for 22375d0.

## Checked and left alone

- **The camera's clock** (9 Oct): within 1 s of the Pi's, every hourly check
  over three days; all 179 camera alarms since 6 Oct matched. It displays UTC
  on purpose (invariant 8), so its overlay reads an hour behind Irish summer
  time until 25 October.
- **Widening the plate band to the right edge** for a pickup that stops very
  close (plate at x 0.94-0.97, 7 Oct 18:35): replayed on stored frames, the
  wider band did not recover those frames -- the plate is cut off by the
  picture's own edge -- and it cost reads that work now (an Audi frame read at
  1.00 read nothing). Not applied.

## Still open

- **The silver pickup (172L66) at the stop.** Its plate is about 200-275 px
  wide there, and when it stops very close the plate sits at the picture's
  edge. On 6 Oct 10:06 it waited ~50 s with `66` misread as `68`/`88`/`61`; on
  7 Oct it was let in at +66 s only when it moved. More pixels on the plate
  at the stopping spot (a small zoom step, or the stop-position re-aim in
  [reolink-rlc-811a.md](../reolink-rlc-811a.md)) is the fix; loosening the
  matching rules is not.
- **The camera's house-side traffic.** Recording the NVR from the camera's
  sub-stream, or from the Pi's relay, would keep a lossy powerline link from
  holding up the camera's delivery to the Pi.
- **The journal** keeps about 36 hours; the evidence for 1-3 October had
  rotated away before it was looked at.
