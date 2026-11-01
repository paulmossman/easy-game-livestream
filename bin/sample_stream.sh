#!/usr/bin/env bash

# Loops a video file into MediaMTX as if it were the phone's RTMP stream.
# The file must be H.264/AAC; it is copied, not re-encoded.

set -euo pipefail

if [[ -z ${1:-} ]]; then
  echo "Usage: ops sample-stream <video-file>" >&2
  exit 1
fi

exec ffmpeg -re -stream_loop -1 -i "$1" -c copy -f flv rtmp://localhost:1935/live/egl
