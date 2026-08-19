"""Execute the fixed loopback RTSP camera-to-gate transcoding pipeline."""

import os


FFMPEG_BINARY = "/usr/bin/ffmpeg"
FFMPEG_COMMAND = (
    FFMPEG_BINARY,
    "-nostdin",
    "-hide_banner",
    "-loglevel", "warning",
    "-rtsp_transport", "tcp",
    "-i", "rtsp://127.0.0.1:8554/camera",
    "-map", "0:v:0",
    "-map", "0:a:0?",
    "-c:v", "copy",
    "-c:a", "libopus",
    "-application", "lowdelay",
    "-frame_duration", "20",
    "-b:a", "24k",
    "-vbr", "constrained",
    "-f", "rtsp",
    "-rtsp_transport", "tcp",
    "rtsp://127.0.0.1:8554/gate",
)


def main() -> None:
    os.execve(FFMPEG_BINARY, list(FFMPEG_COMMAND), {"LANG": "C", "LC_ALL": "C"})


if __name__ == "__main__":
    main()
