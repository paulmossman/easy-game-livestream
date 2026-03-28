import os
import subprocess
import json
import re
import threading
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import parse_qs
from flask import Flask, render_template, request, jsonify, redirect, session, url_for
from flask_socketio import SocketIO, emit
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Load config
config_path = '/app/config/config.json'
if os.path.exists(config_path):
    with open(config_path) as f:
        config = json.load(f)
else:
    config = {}

os.environ.setdefault('OAUTHLIB_INSECURE_TRANSPORT', '1')

state = {
    'home_team': 'Home',
    'home_score': '0',
    'home_pp': False,
    'home_en': False,
    'away_team': 'Away',
    'away_score': '0',
    'away_pp': False,
    'away_en': False,
    'clock_mode': config.get('clock_mode', 'stop_time'),
    'clock_running': False,
    'period': 'Period 1',
    'time': '20:00',
    'mute': False,
    'mute_on_stop': True,
    'incoming_audio_db': None,
    'incoming_audio_label': 'Waiting for stream',
    'incoming_audio_active': False,
}
ffmpeg_process = None
relay_ffmpeg_process = None
process_lock = threading.Lock()
publish_start_generation = 0
ffmpeg_ready = False
muted_volume_level = '0.001'
normal_volume_level = '1.0'
stream_monitor_interval_seconds = float(config.get('stream_monitor_interval_seconds', 1.0))
mediamtx_api_url = config.get('mediamtx_api_url', 'http://mediamtx:9997/v3/paths/list')
last_stream_ready_state = None
audio_control_target = 'volume@audio_gain'
state_lock = threading.Lock()
incoming_audio_monitor_interval_seconds = float(config.get('incoming_audio_monitor_interval_seconds', 15.0))
volume_pattern = re.compile(r'max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB')
config_lock = threading.Lock()
google_oauth_scopes = [
    'https://www.googleapis.com/auth/youtube',
    'https://www.googleapis.com/auth/youtube.force-ssl',
]
google_oauth_state_key = 'youtube_oauth_state'
app_data_dir = '/app/data'
google_token_path = os.path.join(app_data_dir, 'youtube-oauth-token.json')
runtime_youtube_destination_path = os.path.join(app_data_dir, 'youtube-active-destination.json')
google_client_secret_paths = [
    '/app/config/google_oauth_client_secret.json',
    '/app/config/client_secret.json',
]
runtime_youtube_destination = {
    'broadcast_id': None,
    'broadcast_title': None,
    'broadcast_status': None,
    'broadcast_url': None,
    'channel_id': None,
    'channel_title': None,
    'stream_id': None,
    'stream_key': None,
    'ingestion_address': None,
}
primary_font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
overlay_home_text_path = '/tmp/overlay-home.txt'
overlay_away_text_path = '/tmp/overlay-away.txt'
overlay_period_text_path = '/tmp/overlay-period.txt'
overlay_time_text_path = '/tmp/overlay-time.txt'
overlay_mute_text_path = '/tmp/overlay-mute.txt'
last_overlay_signature = None

def ensure_app_data_dir():
    os.makedirs(app_data_dir, exist_ok=True)

def cleanup_ffmpeg():
    global ffmpeg_process, ffmpeg_ready
    ffmpeg_process = None
    ffmpeg_ready = False

def is_ffmpeg_running():
    return ffmpeg_process is not None and ffmpeg_process.poll() is None

def is_relay_ffmpeg_running():
    return relay_ffmpeg_process is not None and relay_ffmpeg_process.poll() is None

def stop_process(process):
    if process:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

def stop_ffmpeg():
    global ffmpeg_process, relay_ffmpeg_process
    stop_process(relay_ffmpeg_process)
    relay_ffmpeg_process = None
    stop_process(ffmpeg_process)
    ffmpeg_process = None

def watch_ffmpeg(process):
    global ffmpeg_process, ffmpeg_ready
    for line in process.stderr:
        # Filter out FFmpeg progress messages
        if "Press [q] to stop" in line or "Output #0, flv, to" in line:
            ffmpeg_ready = True
        if not any(keyword in line for keyword in ["frame=", "fps=", "size=", "time=", "bitrate=", "speed="]):
            print("FFmpeg stderr:", line.strip(), flush=True)

    return_code = process.wait()
    with process_lock:
        if ffmpeg_process is process:
            print(f"FFmpeg exited with code {return_code}", flush=True)
            cleanup_ffmpeg()

def watch_relay_ffmpeg(process):
    global relay_ffmpeg_process
    for line in process.stderr:
        if not any(keyword in line for keyword in ["frame=", "fps=", "size=", "time=", "bitrate=", "speed="]):
            print("Relay FFmpeg stderr:", line.strip(), flush=True)

    return_code = process.wait()
    with process_lock:
        if relay_ffmpeg_process is process:
            print(f"Relay FFmpeg exited with code {return_code}", flush=True)
            relay_ffmpeg_process = None

def current_overlay_text():
    home_flags = ' '.join(flag for flag, enabled in [('PP', state['home_pp']), ('EN', state['home_en'])] if enabled)
    away_flags = ' '.join(flag for flag, enabled in [('PP', state['away_pp']), ('EN', state['away_en'])] if enabled)
    home_display = f"{state['home_team']}: {state['home_score']}" + (f" {home_flags}" if home_flags else '')
    away_display = ((f"{away_flags} " if away_flags else '') + f"{state['away_team']}: {state['away_score']}")
    return {
        'home': home_display,
        'away': away_display,
        'period': state['period'],
        'time': state['time'],
        'mute': 'MUTED' if state['mute'] else '',
    }

def write_text_file(path, text):
    with open(path, 'w', encoding='utf-8') as text_file:
        text_file.write(text)

def overlay_signature(overlay_text):
    return (
        overlay_text['home'],
        overlay_text['away'],
        overlay_text['period'],
        overlay_text['time'],
        overlay_text['mute'],
    )

def write_overlay_text(force=False):
    global last_overlay_signature
    overlay_text = current_overlay_text()
    current_signature = overlay_signature(overlay_text)

    if not force and current_signature == last_overlay_signature:
        return False

    write_text_file(overlay_home_text_path, overlay_text['home'])
    write_text_file(overlay_away_text_path, overlay_text['away'])
    write_text_file(overlay_period_text_path, overlay_text['period'])
    write_text_file(overlay_time_text_path, overlay_text['time'])
    write_text_file(overlay_mute_text_path, overlay_text['mute'])
    last_overlay_signature = current_signature
    print("Overlay text updated", flush=True)
    return True

def current_state_payload():
    return dict(state)

def normalize_score_value(value):
    try:
        return str(max(0, int(value)))
    except (TypeError, ValueError):
        return '0'

def parse_clock(clock_text):
    try:
        minutes_text, seconds_text = str(clock_text).split(':', 1)
        minutes = int(minutes_text)
        seconds = int(seconds_text)
        if minutes < 0 or seconds < 0 or seconds > 59:
            raise ValueError
        return (minutes * 60) + seconds
    except (ValueError, AttributeError):
        return None

def format_clock(total_seconds):
    total_seconds = max(0, int(total_seconds))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"

def current_volume_level():
    return muted_volume_level if state['mute'] else normal_volume_level

def send_ffmpeg_stdin_command(command):
    if not is_ffmpeg_running() or ffmpeg_process.stdin is None:
        print("FFmpeg stdin command skipped because FFmpeg is not ready", flush=True)
        return False

    try:
        # FFmpeg expects `c` as a single interactive keystroke, then the
        # command line separately after it prints the prompt.
        ffmpeg_process.stdin.write('c')
        ffmpeg_process.stdin.flush()
        time.sleep(0.05)
        ffmpeg_process.stdin.write(f'{command}\n')
        ffmpeg_process.stdin.flush()
        print(f"Sent FFmpeg stdin command: {command}", flush=True)
        return True
    except (BrokenPipeError, OSError) as e:
        print(f"FFmpeg stdin command failed: {e}", flush=True)
        return False

def update_live_volume():
    command = f'{audio_control_target} -1 volume {current_volume_level()}'
    return send_ffmpeg_stdin_command(command)

def tick_game_clock():
    while True:
        time.sleep(1)
        with state_lock:
            if not state['clock_running']:
                continue
            remaining_seconds = parse_clock(state['time'])
            if remaining_seconds is None:
                continue
            if remaining_seconds <= 0:
                state['clock_running'] = False
                if state['clock_mode'] == 'stop_time' and state['mute_on_stop']:
                    state['mute'] = True
                updated_state = current_state_payload()
            else:
                state['time'] = format_clock(remaining_seconds - 1)
                if parse_clock(state['time']) == 0:
                    state['clock_running'] = False
                    if state['clock_mode'] == 'stop_time' and state['mute_on_stop']:
                        state['mute'] = True
                updated_state = current_state_payload()
        write_overlay_text()
        socketio.emit('state_updated', current_state_payload())

def current_input_url():
    return f"{config.get('rtmp_input_url', 'rtmp://mediamtx/live')}/{config.get('stream_name', 'stream')}"

def build_rtmp_url(base_url, stream_key):
    if not stream_key:
        return base_url
    if base_url.endswith('/'):
        return f"{base_url}{stream_key}"
    return f"{base_url}/{stream_key}"

def current_preview_output_url():
    output_base = config.get('preview_output_url') or config.get('output_url') or 'rtmp://mediamtx/live/'
    output_key = config.get('preview_stream_key') or config.get('output_stream_key') or 'preview_hls'
    return build_rtmp_url(output_base, output_key)

def current_webrtc_preview_output_url():
    output_base = config.get('webrtc_preview_output_url') or 'rtsp://mediamtx:8554/live/'
    output_key = config.get('webrtc_preview_stream_key') or 'preview'
    return build_rtmp_url(output_base, output_key)

def current_upstream_output_url():
    if runtime_youtube_destination.get('broadcast_id'):
        if runtime_youtube_destination.get('broadcast_status') == 'complete':
            return ''
        output_base = runtime_youtube_destination['ingestion_address'] or config.get('youtube_output_url') or ''
        output_key = runtime_youtube_destination['stream_key'] or ''
    else:
        output_base = config.get('youtube_output_url') or ''
        output_key = os.getenv('YOUTUBE_STREAM_KEY') or config.get('youtube_stream_key') or ''
    if not output_base:
        return ''
    return build_rtmp_url(output_base, output_key)

def save_config():
    with config_lock:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
            f.write('\n')

def save_runtime_youtube_destination():
    ensure_app_data_dir()
    with open(runtime_youtube_destination_path, 'w', encoding='utf-8') as f:
        json.dump(runtime_youtube_destination, f, indent=4)
        f.write('\n')

def load_runtime_youtube_destination():
    if not os.path.exists(runtime_youtube_destination_path):
        return

    try:
        with open(runtime_youtube_destination_path, encoding='utf-8') as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Could not load saved YouTube destination: {e}", flush=True)
        return

    if not isinstance(payload, dict):
        return

    for key in runtime_youtube_destination:
        runtime_youtube_destination[key] = payload.get(key)

def config_is_writable():
    return os.access(os.path.dirname(config_path), os.W_OK)

def youtube_title_for_today(home_team=None, away_team=None):
    if home_team is None or away_team is None:
        with state_lock:
            home_team = home_team or state.get('home_team') or 'Home'
            away_team = away_team or state.get('away_team') or 'Away'
    return f"{home_team} vs {away_team} - {datetime.now().date().isoformat()}"

def youtube_status_snapshot():
    active_destination = (
        dict(runtime_youtube_destination)
        if runtime_youtube_destination.get('broadcast_id') and runtime_youtube_destination.get('broadcast_status') != 'complete'
        else {}
    )
    return {
        'authorized': os.path.exists(google_token_path),
        'oauth_configured': google_client_config() is not None,
        'channel_choices': [],
        'active_destination': active_destination,
        'can_stop': bool(runtime_youtube_destination.get('broadcast_id') and runtime_youtube_destination.get('broadcast_status') != 'complete'),
        'configured_stream_key': bool(os.getenv('YOUTUBE_STREAM_KEY') or config.get('youtube_stream_key')),
    }

def google_client_config():
    config_client_id = config.get('google_oauth_client_id')
    config_client_secret = config.get('google_oauth_client_secret')
    if config_client_id and config_client_secret:
        return {
            'web': {
                'client_id': config_client_id,
                'client_secret': config_client_secret,
                'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                'token_uri': 'https://oauth2.googleapis.com/token',
                'redirect_uris': [config.get('google_oauth_redirect_uri', '')],
            }
        }

    for client_secret_path in google_client_secret_paths:
        if os.path.exists(client_secret_path):
            with open(client_secret_path, encoding='utf-8') as f:
                return json.load(f)

    return None

def google_redirect_uri():
    configured = config.get('google_oauth_redirect_uri')
    if configured:
        return configured
    return url_for('youtube_oauth_callback', _external=True)

def load_google_credentials():
    if not os.path.exists(google_token_path):
        return None
    credentials = Credentials.from_authorized_user_file(google_token_path, google_oauth_scopes)
    if not credentials.valid and credentials.refresh_token:
        credentials.refresh(GoogleAuthRequest())
        save_google_credentials(credentials)
    if not credentials.valid:
        return None
    return credentials

def save_google_credentials(credentials):
    ensure_app_data_dir()
    with open(google_token_path, 'w', encoding='utf-8') as f:
        f.write(credentials.to_json())

def youtube_service(credentials=None):
    credentials = credentials or load_google_credentials()
    if credentials is None:
        raise RuntimeError('YouTube is not authorized')
    return build('youtube', 'v3', credentials=credentials, cache_discovery=False)

def youtube_channel_choices(service):
    response = service.channels().list(
        part='id,snippet',
        mine=True,
        maxResults=50,
    ).execute()
    choices = []
    for item in response.get('items', []):
        snippet = item.get('snippet') or {}
        choices.append({
            'id': item.get('id'),
            'title': snippet.get('title') or 'Untitled channel',
            'thumbnail_url': (((snippet.get('thumbnails') or {}).get('default') or {}).get('url')),
        })
    return choices

def restart_relay_ffmpeg():
    global relay_ffmpeg_process
    with process_lock:
        if relay_ffmpeg_process is not None:
            stop_process(relay_ffmpeg_process)
            relay_ffmpeg_process = None
        if is_ffmpeg_running():
            start_relay_ffmpeg()

def create_youtube_broadcast_for_channel(channel_id, home_team=None, away_team=None):
    service = youtube_service()
    channels = youtube_channel_choices(service)
    selected_channel = next((item for item in channels if item['id'] == channel_id), None)
    if selected_channel is None:
        raise ValueError('Selected YouTube channel is no longer available')

    now = datetime.now(timezone.utc).replace(microsecond=0)
    title = youtube_title_for_today(home_team=home_team, away_team=away_team)
    latency = config.get('youtube_latency_preference', 'ultraLow')
    privacy = config.get('youtube_privacy_status', 'public')

    stream_response = service.liveStreams().insert(
        part='snippet,cdn,contentDetails,status',
        body={
            'snippet': {
                'title': title,
                'description': f'Created by easy-game-livestream for {selected_channel["title"]}',
            },
            'cdn': {
                'frameRate': '30fps',
                'ingestionType': 'rtmp',
                'resolution': '720p',
            },
            'contentDetails': {
                'isReusable': False,
            },
        },
    ).execute()

    broadcast_response = service.liveBroadcasts().insert(
        part='snippet,status,contentDetails',
        body={
            'snippet': {
                'title': title,
                'scheduledStartTime': now.isoformat().replace('+00:00', 'Z'),
            },
            'status': {
                'privacyStatus': privacy,
                'selfDeclaredMadeForKids': False,
            },
            'contentDetails': {
                'enableAutoStart': True,
                'enableAutoStop': True,
                'latencyPreference': latency,
            },
        },
    ).execute()

    service.liveBroadcasts().bind(
        part='id,contentDetails',
        id=broadcast_response['id'],
        streamId=stream_response['id'],
    ).execute()

    ingestion_info = ((stream_response.get('cdn') or {}).get('ingestionInfo') or {})
    runtime_youtube_destination.update({
        'broadcast_id': broadcast_response.get('id'),
        'broadcast_title': title,
        'broadcast_status': ((broadcast_response.get('status') or {}).get('lifeCycleStatus')) or 'created',
        'broadcast_url': f"https://www.youtube.com/watch?v={broadcast_response.get('id')}",
        'channel_id': selected_channel['id'],
        'channel_title': selected_channel['title'],
        'stream_id': stream_response.get('id'),
        'stream_key': ingestion_info.get('streamName'),
        'ingestion_address': ingestion_info.get('ingestionAddress') or config.get('youtube_output_url'),
    })

    if runtime_youtube_destination['stream_key']:
        save_runtime_youtube_destination()
        if config_is_writable():
            config['youtube_stream_key'] = runtime_youtube_destination['stream_key']
            if runtime_youtube_destination['ingestion_address']:
                config['youtube_output_url'] = runtime_youtube_destination['ingestion_address']
            save_config()
        restart_relay_ffmpeg()

    return {
        'title': title,
        'broadcast_id': runtime_youtube_destination['broadcast_id'],
        'broadcast_url': runtime_youtube_destination['broadcast_url'],
        'channel_id': runtime_youtube_destination['channel_id'],
        'channel_title': runtime_youtube_destination['channel_title'],
    }

def update_active_youtube_broadcast_title(home_team=None, away_team=None):
    broadcast_id = runtime_youtube_destination.get('broadcast_id')
    if not broadcast_id or runtime_youtube_destination.get('broadcast_status') == 'complete':
        return None

    service = youtube_service()
    response = service.liveBroadcasts().list(
        part='snippet,status,contentDetails',
        id=broadcast_id,
    ).execute()
    items = response.get('items') or []
    if not items:
        raise ValueError('Active YouTube broadcast could not be found')

    item = items[0]
    title = youtube_title_for_today(home_team=home_team, away_team=away_team)
    snippet = dict(item.get('snippet') or {})
    snippet['title'] = title

    updated = service.liveBroadcasts().update(
        part='snippet,status,contentDetails',
        body={
            'id': broadcast_id,
            'snippet': snippet,
            'status': item.get('status') or {},
            'contentDetails': item.get('contentDetails') or {},
        },
    ).execute()

    runtime_youtube_destination['broadcast_title'] = title
    runtime_youtube_destination['broadcast_status'] = ((updated.get('status') or {}).get('lifeCycleStatus')) or runtime_youtube_destination.get('broadcast_status')
    save_runtime_youtube_destination()
    return title

def stop_active_youtube_broadcast():
    broadcast_id = runtime_youtube_destination.get('broadcast_id')
    if not broadcast_id:
        raise ValueError('No active YouTube broadcast is available to stop')
    if runtime_youtube_destination.get('broadcast_status') == 'complete':
        return dict(runtime_youtube_destination)

    service = youtube_service()
    transitioned = service.liveBroadcasts().transition(
        part='status',
        id=broadcast_id,
        broadcastStatus='complete',
    ).execute()

    runtime_youtube_destination['broadcast_status'] = ((transitioned.get('status') or {}).get('lifeCycleStatus')) or 'complete'
    save_runtime_youtube_destination()

    global relay_ffmpeg_process
    with process_lock:
        if relay_ffmpeg_process is not None:
            stop_process(relay_ffmpeg_process)
            relay_ffmpeg_process = None

    return dict(runtime_youtube_destination)

def measure_input_audio_level():
    cmd = [
        'ffmpeg',
        '-nostats',
        '-t', '2',
        '-i', current_input_url(),
        '-af', 'volumedetect',
        '-vn',
        '-sn',
        '-dn',
        '-f', 'null',
        '-',
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=6,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None

    match = volume_pattern.search(result.stderr or '')
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None

def audio_level_status(audio_db):
    if audio_db is None:
        return ('Waiting for stream', False)
    if audio_db <= -80:
        return (f'Silent ({audio_db:.0f} dB)', False)
    return (f'Active ({audio_db:.0f} dB)', True)

def monitor_input_audio():
    last_snapshot = (None, None, None)
    while True:
        ready = is_stream_ready()
        audio_db = measure_input_audio_level() if ready else None
        label, is_active = audio_level_status(audio_db)
        snapshot = (audio_db, label, is_active)
        if snapshot != last_snapshot:
            with state_lock:
                state['incoming_audio_db'] = audio_db
                state['incoming_audio_label'] = label
                state['incoming_audio_active'] = is_active
            socketio.emit('state_updated', current_state_payload())
            last_snapshot = snapshot
        time.sleep(incoming_audio_monitor_interval_seconds)

def current_stream_path():
    return stream_path_from_url(current_input_url())

def stream_path_from_url(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.path.strip('/')

def mediamtx_path_ready(stream_path):
    global last_stream_ready_state
    if not stream_path:
        return False

    try:
        with urllib.request.urlopen(mediamtx_api_url, timeout=2) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        if last_stream_ready_state is not False:
            print(f"MediaMTX API unavailable: {e}", flush=True)
            last_stream_ready_state = False
        return False

    for item in payload.get('items', []):
        if not item:
            continue
        if item.get('name') != stream_path:
            continue
        ready = bool(item.get('ready'))
        if ready != last_stream_ready_state:
            print(f"MediaMTX path {stream_path} ready={ready}", flush=True)
            last_stream_ready_state = ready
        return ready

    if last_stream_ready_state is not False:
        print(f"MediaMTX path {stream_path} is not present", flush=True)
        last_stream_ready_state = False
    return False

def is_stream_ready():
    return mediamtx_path_ready(current_stream_path())

def start_relay_ffmpeg():
    global relay_ffmpeg_process
    upstream_url = current_upstream_output_url()
    if not upstream_url:
        return
    if is_relay_ffmpeg_running():
        return

    input_url = current_preview_output_url()
    if not mediamtx_path_ready(stream_path_from_url(input_url)):
        return

    try:
        relay_cmd = [
            'ffmpeg',
            '-rtmp_live', 'live',
            '-i', input_url,
            '-c', 'copy',
            '-f', 'flv',
            upstream_url,
        ]
        print("Starting relay FFmpeg:", ' '.join(relay_cmd), flush=True)
        relay_ffmpeg_process = subprocess.Popen(
            relay_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        threading.Thread(target=watch_relay_ffmpeg, args=(relay_ffmpeg_process,), daemon=True).start()
    except Exception as e:
        print(f"Error starting relay FFmpeg: {e}", flush=True)
        relay_ffmpeg_process = None

def run_ffmpeg():
    global ffmpeg_process, ffmpeg_ready
    input_url = current_input_url()
    preview_output_url = current_preview_output_url()
    webrtc_output_url = current_webrtc_preview_output_url()
    
    try:
        stop_ffmpeg()
        ffmpeg_ready = False
        write_overlay_text(force=True)
        filter_complex = ';'.join([
            f"[0:v]drawtext=fontfile={primary_font_path}:textfile={overlay_home_text_path}:reload=1:fontcolor=white:fontsize=38:box=1:boxcolor=black@0.69:boxborderw=18:x=18:y=18[v_home]",
            f"[v_home]drawtext=fontfile={primary_font_path}:textfile={overlay_away_text_path}:reload=1:fontcolor=white:fontsize=38:box=1:boxcolor=black@0.69:boxborderw=18:x=w-tw-36:y=18[v_away]",
            f"[v_away]drawtext=fontfile={primary_font_path}:textfile={overlay_time_text_path}:reload=1:fontcolor=white:fontsize=44:box=1:boxcolor=black@0.69:boxborderw=18:x=(w-tw)/2:y=18[v_time]",
            f"[v_time]drawtext=fontfile={primary_font_path}:textfile={overlay_period_text_path}:reload=1:fontcolor=white:fontsize=32:box=1:boxcolor=black@0.69:boxborderw=18:x=18:y=h-th-36[v_period]",
            f"[v_period]drawtext=fontfile={primary_font_path}:textfile={overlay_mute_text_path}:reload=1:fontcolor=white@0.76:fontsize=38:x=w-tw-34:y=h-th-72[v_composited]",
            '[v_composited]split=2[v_preview][v_webrtc]',
            f'[0:a]{audio_control_target}=volume={current_volume_level()},asplit=2[a_preview][a_webrtc]',
        ])
        cmd = [
            'ffmpeg',
            '-rtmp_live', 'live',
            '-i', input_url,
            '-filter_complex', filter_complex,
            '-map', '[v_preview]',
            '-map', '[a_preview]',
            '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p',
            '-preset', 'veryfast',
            '-tune', 'zerolatency',
            '-g', '30',
            '-keyint_min', '30',
            '-sc_threshold', '0',
            '-bf', '0',
            '-c:a', 'aac',
            '-f', 'flv',
            preview_output_url,
            '-map', '[v_webrtc]',
            '-map', '[a_webrtc]',
            '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p',
            '-preset', 'veryfast',
            '-tune', 'zerolatency',
            '-g', '30',
            '-keyint_min', '30',
            '-sc_threshold', '0',
            '-bf', '0',
            '-c:a', 'libopus',
            '-b:a', '96k',
            '-application', 'lowdelay',
            '-f', 'rtsp',
            '-rtsp_transport', 'tcp',
            webrtc_output_url,
        ]
        print("Starting ffmpeg:", ' '.join(cmd), flush=True)
        ffmpeg_process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        
        time.sleep(5)
        if ffmpeg_process.poll() is not None:
            print(f"FFmpeg exited early with code {ffmpeg_process.returncode}", flush=True)
            cleanup_ffmpeg()
            return

        threading.Thread(target=watch_ffmpeg, args=(ffmpeg_process,), daemon=True).start()
        start_relay_ffmpeg()
    except Exception as e:
        print(f"Error starting ffmpeg: {e}", flush=True)
        cleanup_ffmpeg()

def monitor_stream():
    while True:
        ready = is_stream_ready()
        with process_lock:
            running = is_ffmpeg_running()
            if ready and not running:
                print("MediaMTX stream is ready; starting FFmpeg", flush=True)
                run_ffmpeg()
            elif not ready and running:
                print("MediaMTX stream is not ready; stopping FFmpeg", flush=True)
                stop_ffmpeg()
            elif running and not is_relay_ffmpeg_running():
                start_relay_ffmpeg()
        time.sleep(stream_monitor_interval_seconds)

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY') or config.get('flask_secret_key') or 'easy-game-livestream-dev-secret'
socketio = SocketIO(app, cors_allowed_origins="*")

def apply_overlay_update(data):
    global state
    with state_lock:
        previous_mute = state['mute']
        previous_clock_mode = state['clock_mode']
        previous_home_team = state['home_team']
        previous_away_team = state['away_team']
        if 'home_score' in data:
            data['home_score'] = normalize_score_value(data['home_score'])
        if 'away_score' in data:
            data['away_score'] = normalize_score_value(data['away_score'])
        if 'clock_mode' in data and data['clock_mode'] not in ('stop_time', 'run_time'):
            data['clock_mode'] = 'stop_time'
        state.update(data)
        if state['clock_mode'] != previous_clock_mode:
            state['clock_running'] = False
        if state['clock_mode'] == 'run_time' and ('period' in data or 'time' in data):
            state['clock_running'] = False
        if 'mute_on_stop' in data and state['clock_mode'] == 'stop_time' and state['mute_on_stop'] and not state['clock_running']:
            state['mute'] = True
        if 'clock_running' in data:
            state['clock_running'] = bool(data['clock_running'])
            if state['clock_mode'] == 'stop_time' and state['mute_on_stop']:
                state['mute'] = not state['clock_running']
        updated_state = current_state_payload()
    print("Updating overlay with data:", data, flush=True)
    if updated_state['mute'] != previous_mute:
        with process_lock:
            if is_ffmpeg_running() and ffmpeg_ready:
                if not update_live_volume():
                    print("Live volume update failed; keeping FFmpeg running", flush=True)
            else:
                print("FFmpeg not ready; mute state will apply on next FFmpeg start", flush=True)
    if (
        runtime_youtube_destination.get('broadcast_id')
        and runtime_youtube_destination.get('broadcast_status') != 'complete'
        and (
            updated_state['home_team'] != previous_home_team
            or updated_state['away_team'] != previous_away_team
        )
    ):
        try:
            updated_title = update_active_youtube_broadcast_title(
                home_team=updated_state['home_team'],
                away_team=updated_state['away_team'],
            )
            if updated_title:
                print(f"Updated active YouTube broadcast title to: {updated_title}", flush=True)
        except Exception as e:
            print(f"Could not update active YouTube broadcast title: {e}", flush=True)
    write_overlay_text()
    updated_state = current_state_payload()
    socketio.emit('state_updated', updated_state)
    return updated_state

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/state', methods=['GET'])
def get_state():
    return jsonify(current_state_payload())

@app.route('/api/state', methods=['POST'])
def update_state():
    data = request.get_json(silent=True) or request.form.to_dict()
    if 'mute' in data and not isinstance(data['mute'], bool):
        data['mute'] = str(data['mute']).lower() in ('1', 'true', 'yes', 'on')
    for key in ('home_pp', 'home_en', 'away_pp', 'away_en', 'mute_on_stop'):
        if key in data and not isinstance(data[key], bool):
            data[key] = str(data[key]).lower() in ('1', 'true', 'yes', 'on')
    if 'clock_running' in data and not isinstance(data['clock_running'], bool):
        data['clock_running'] = str(data['clock_running']).lower() in ('1', 'true', 'yes', 'on')
    updated_state = apply_overlay_update(data)
    return jsonify(updated_state)

@app.route('/api/youtube/status', methods=['GET'])
def youtube_status():
    snapshot = youtube_status_snapshot()
    try:
        service = youtube_service()
        snapshot['authorized'] = True
        snapshot['channel_choices'] = youtube_channel_choices(service)
    except Exception as e:
        snapshot['authorized'] = False
        snapshot['authorization_error'] = str(e)
    return jsonify(snapshot)

@app.route('/api/youtube/oauth/start', methods=['GET'])
def youtube_oauth_start():
    client_config = google_client_config()
    if client_config is None:
        return jsonify({
            'error': 'Missing Google OAuth client configuration. Add google_oauth_client_secret.json to config/ or set google_oauth_client_id/google_oauth_client_secret in config.json.'
        }), 400

    flow = Flow.from_client_config(client_config, scopes=google_oauth_scopes)
    flow.redirect_uri = google_redirect_uri()
    authorization_url, state_value = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent',
    )
    session[google_oauth_state_key] = state_value
    return redirect(authorization_url)

@app.route('/api/youtube/oauth/callback', methods=['GET'])
def youtube_oauth_callback():
    client_config = google_client_config()
    if client_config is None:
        return 'Google OAuth client configuration is missing.', 400

    stored_state = session.get(google_oauth_state_key)
    if not stored_state:
        return 'OAuth state was not found. Please try Create New Stream again.', 400

    flow = Flow.from_client_config(client_config, scopes=google_oauth_scopes, state=stored_state)
    flow.redirect_uri = google_redirect_uri()
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception as e:
        return f'Google OAuth failed: {e}', 400

    save_google_credentials(flow.credentials)
    session.pop(google_oauth_state_key, None)
    return '''
<!DOCTYPE html>
<html lang="en">
<body>
<script>
if (window.opener) {
  window.opener.postMessage({ type: 'youtube-oauth-complete' }, window.location.origin);
}
window.close();
</script>
YouTube login complete. You can close this window.
</body>
</html>
'''

@app.route('/api/youtube/create-stream', methods=['POST'])
def youtube_create_stream():
    data = request.get_json(silent=True) or {}
    channel_id = data.get('channel_id')
    if not channel_id:
        return jsonify({'error': 'channel_id is required'}), 400

    home_team = (data.get('home_team') or '').strip() or None
    away_team = (data.get('away_team') or '').strip() or None

    try:
        created = create_youtube_broadcast_for_channel(
            channel_id,
            home_team=home_team,
            away_team=away_team,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except HttpError as e:
        return jsonify({'error': f'YouTube API error: {e}'}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify(created)

@app.route('/api/youtube/stop-stream', methods=['POST'])
def youtube_stop_stream():
    try:
        stopped = stop_active_youtube_broadcast()
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except HttpError as e:
        return jsonify({'error': f'YouTube API error: {e}'}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify(stopped)

@app.route('/on_publish', methods=['GET', 'POST'])
def on_publish():
    raw_data = request.get_data(as_text=True)
    parsed_body = parse_qs(raw_data)
    print(f"on_publish method: {request.method}", flush=True)
    print(f"on_publish args: {request.args}", flush=True)
    print(f"on_publish form: {request.form}", flush=True)
    print(f"on_publish raw_data: {raw_data}", flush=True)
    stream = (
        request.form.get('name')
        or request.args.get('name')
        or request.form.get('stream')
        or request.args.get('stream')
        or parsed_body.get('name', [None])[0]
        or parsed_body.get('stream', [None])[0]
    )
    print(f"Extracted stream: {stream}", flush=True)
    print("Ignoring legacy on_publish callback; MediaMTX API monitor handles stream readiness", flush=True)
    return 'OK'

@app.route('/on_publish_done', methods=['GET', 'POST'])
def on_publish_done():
    raw_data = request.get_data(as_text=True)
    print(f"on_publish_done method: {request.method}", flush=True)
    print(f"on_publish_done args: {request.args}", flush=True)
    print(f"on_publish_done form: {request.form}", flush=True)
    print(f"on_publish_done raw_data: {raw_data}", flush=True)
    print("Ignoring legacy on_publish_done callback; MediaMTX API monitor handles stream readiness", flush=True)
    return 'OK'

@socketio.on('update_overlay')
def handle_update_overlay(data):
    updated_state = apply_overlay_update(data)
    emit('state_updated', updated_state, broadcast=True)

if __name__ == '__main__':
    ensure_app_data_dir()
    load_runtime_youtube_destination()
    write_overlay_text()
    threading.Thread(target=monitor_stream, daemon=True).start()
    threading.Thread(target=tick_game_clock, daemon=True).start()
    threading.Thread(target=monitor_input_audio, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
