#!/bin/bash
# decode.sh NAME FILE (workstation only, never the Pi) : decode the whole NVR file at each width exactly as the Pi's
# session decoder does (scale=w='min(iw,W)':h=-2, mjpeg -q:v 2, -fps_mode passthrough -enc_time_base demux).
set -e
N=$1; IN=${2:?nvr file}; OUT=${DW_ROOT:-.}/frames/$N
mkdir -p $OUT
ffprobe -v error -select_streams v:0 -show_entries frame=pts_time,best_effort_timestamp_time -of csv=p=0 $IN > $OUT/pts.csv 2>/dev/null || true
for W in 1920 2560 3200; do
  mkdir -p $OUT/$W
  ffmpeg -nostdin -hide_banner -loglevel fatal -i $IN -map 0:v:0 -an \
    -vf "scale=w='min(iw,$W)':h=-2" -fps_mode passthrough -enc_time_base demux -q:v 2 -c:v mjpeg -f image2 $OUT/$W/%05d.jpg || true
done
for W in 1920 2560 3200; do echo "$W $(ls $OUT/$W | wc -l)"; done; wc -l $OUT/pts.csv
