# easy-game-livestream

Layer game score, time, etc onto an RTMP livestream.

## Setup

1. Set your YouTube stream key in `config/config.json` or as environment variable `YOUTUBE_STREAM_KEY`.
2. Run `docker-compose up --build`
3. Open http://localhost:5001 for control interface.
4. Configure PRISM Live Studio on iPhone to stream to `rtmp://<Docker-Host>:1935/live` with stream key set to the value in `config/config.json` (currently "bogus")
5. Open a local preview of the overlaid stream at `http://<Docker-Host>:8889/live/preview/?muted=no` for lower-latency WebRTC playback with audio, or `http://<Docker-Host>:8888/live/preview_hls/index.m3u8` for HLS playback. WebRTC also requires UDP port `8189` to be reachable.

## YouTube Studio Flow

Use this when you want the app to publish to YouTube with the configured stream key.

1. Open [YouTube Studio](https://studio.youtube.com/).
2. Click `Create`, then `Go live`.
3. If YouTube asks whether you want to stream right now or schedule for later, choose the option that gets you into Live Control Room for a stream.
4. In Live Control Room, confirm the selected stream key matches the one in `config/config.json`.
5. Set the stream title in YouTube Studio. A good format is `<Home Team> vs <Away Team> - YYYY-MM-DD`.
6. Set the visibility, audience, and any other YouTube options you care about.
7. Leave that YouTube Live Control Room page open.
8. Start the incoming stream from PRISM to this app.
9. Check the local preview at `http://localhost:5001` or `http://localhost:8889/live/preview/?muted=no`.
10. Wait for YouTube Studio to show that it is receiving the stream.
11. In YouTube Studio, click `Go live` when you are ready for the broadcast to be public.

This manual flow does not require a Google login inside the app. The app only needs the reusable `youtube_stream_key` in `config/config.json`.

## Optional Google Login Flow

The Web UI's `Create New Stream` feature is separate from the manual YouTube Studio flow above.

It only works if you explicitly configure Google OAuth for this app. Without that configuration, the app should stay in manual YouTube Studio mode and you should ignore `Create New Stream`.

### Google Cloud Setup

Use these steps only if you want to try the optional `Create New Stream` flow in the Web UI.

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new Google Cloud project, or select an existing one that you want to use for this app.
3. In that project, open `APIs & Services`, then `Library`.
4. Search for `YouTube Data API v3` and enable it.
5. From `APIs & Services`, got to `OAuth consent screen` and click `Get Started`.
6. If prompted, choose the app type that fits your account. For personal testing, the flow is usually easiest with an `External` app in testing mode.
7. Fill in the required consent screen fields:
   - App name: something like `Easy Game Livestream`
   - User support email: your email
   - Developer contact email: your email
8. Save the consent screen settings.
10. If Google shows a `Test users` step and your app is still in testing mode, add the Google account(s) you will log in with.
11. Go to `APIs & Services`, then `Credentials`.
12. Click `Create Credentials`, then `OAuth client ID`.
13. Choose `Web application`.
14. Give the client a name, such as `Easy Game Livestream`.
15. Under `Authorized redirect URIs`, add this exact URL:
    - `http://localhost:5001/api/youtube/oauth/callback`
16. Create the client.
17. Download the OAuth client JSON file from Google Cloud.
18. Save that downloaded JSON file as:
    - `config/google_oauth_client_secret.json`
9. Under `Data Access`, add the following YouTube Scopes:
   - https://www.googleapis.com/auth/youtube
   - https://www.googleapis.com/auth/youtube.force-ssl
1. Under `Audience` → Test users → + Add users, add yourself.
19. Restart the app:
    - `docker compose up --build -d`
20. Open `http://localhost:5001` and try `Create New Stream` again.

### Notes

PWM Create New Stream - each time
- "Choose your account or a brand account" --> Your channel.
- Your app is in test mode so you'll get a "Google hasn’t verified this app" warning. Click "Continue".
PWM 1st:
- The first time you try to create a new stream you'll be notified that your app wants to "wants access to your Google Account". Click "Select all" and click "Continue".
   - Afterwards: "Easy Game Livestream already has some access ..." Click "Continue".


- The callback URL must match exactly: `http://localhost:5001/api/youtube/oauth/callback`
- If Google shows `redirect_uri_mismatch`, the redirect URI in Google Cloud does not exactly match the one above.
- The app stores the resulting OAuth token locally in the container at `/tmp/youtube-oauth-token.json` so you do not need to sign in again until the container is recreated.
- If `config/config.json` is mounted read-only, OAuth-created YouTube streams still work for the current container session, but the created stream key will not be written back into that file.

## Quick Checklist

Before clicking `Go live` in YouTube Studio, verify:

- YouTube Studio is open to the correct live session.
- The stream key in YouTube Studio matches `youtube_stream_key`.
- The local preview has video and audio.
- The scoreboard text looks right.
- The app is un-muted if you want program audio on the stream.

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

If you use the optional Web UI `Create New Stream` flow, you can also set `youtube_privacy_status` in `config/config.json` to control the visibility of newly created YouTube broadcasts. Supported values are `public`, `unlisted`, and `private`. If you do not set it, the app currently defaults to `public`. This setting does not affect the manual YouTube Studio flow, since visibility is chosen directly in YouTube Studio there.
