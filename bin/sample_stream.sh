#!/usr/bin/env bash

# Loops a video file into MediaMTX as if it were the phone's RTMP stream.
# The video must be H.264; it is copied, not re-encoded. The audio is re-encoded
# because some files have AAC with no codec config (no AudioSpecificConfig), and
# MediaMTX drops an RTMP publisher whose audio track it can't set up.

set -euo pipefail

if [[ -z ${1:-} ]]; then
  echo "Usage: ops sample-stream <video-file>" >&2
  exit 1
fi

exec ffmpeg -re -stream_loop -1 -i "$1" -c:v copy -c:a aac -b:a 128k -f flv rtmp://localhost:1935/live/egl
