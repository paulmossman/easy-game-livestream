# easy-game-livestream

Layer game score, time, etc onto an RTMP livestream.

## Setup

1. Set your YouTube stream key in `config/config.json` or as environment variable `YOUTUBE_STREAM_KEY`.

2. Run `docker-compose up --build`

3. Open http://localhost:5001 for control interface.

4. Configure PRISM Live Studio on iPhone to stream to `rtmp://<Docker-Host>:1935/live` with stream key set to the value in `config/config.json` (currently "bogus")
5. Open a local preview of the overlaid stream at `http://<Docker-Host>:8889/live/preview/?muted=no` for lower-latency WebRTC playback with audio, or `http://<Docker-Host>:8888/live/preview_hls/index.m3u8` for HLS playback. WebRTC also requires UDP port `8189` to be reachable.

## Features

- Accepts RTMP stream from iPhone
- Adds dynamic text overlay with score, period, time
- Mutes/un-mutes audio
- Web interface for real-time control
- Forwards to YouTube livestream
- Local browser preview through MediaMTX, including low-latency WebRTC audio

## How It Works

The application uses MediaMTX to receive the RTMP stream from PRISM, and ffmpeg reads that stream, adds text overlays, and publishes both an AAC RTMP/HLS program feed and an Opus RTSP WebRTC preview feed.

Use `preview_output_url` / `preview_stream_key` for the local RTMP/HLS program feed, `webrtc_preview_output_url` / `webrtc_preview_stream_key` for the low-latency WebRTC preview target, and `youtube_output_url` / `youtube_stream_key` for the upstream RTMP destination.
