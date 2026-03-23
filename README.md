# easy-game-livestream

Layer game score, time, etc onto an RTMP livestream.

## Setup

1. Set your YouTube stream key in `config/config.json` or as environment variable `YOUTUBE_STREAM_KEY`.

2. Run `docker-compose up --build`

3. Open http://localhost:5001 for control interface.

4. Configure PRISM Live Studio on iPhone to stream to `rtmp://<Docker-Host>:1935/live` with stream key set to the value in `config/config.json` (currently "bogus")

## Features

- Accepts RTMP stream from iPhone
- Adds dynamic text overlay with score, period, time
- Mutes/un-mutes audio
- Web interface for real-time control
- Forwards to YouTube livestream

## How It Works

The application uses MediaMTX to receive the RTMP stream from PRISM, and ffmpeg reads that stream, adds text overlays, and forwards it to the configured output.