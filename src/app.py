import os
import subprocess
import json
import re
import sqlite3
import threading
import time
import tempfile
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import parse_qs
import yaml
from flask import Flask, render_template, request, jsonify, redirect, session, url_for
from flask_socketio import SocketIO, emit
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Load config
config_path = '/app/config/config.yml'
default_config_path = '/app/config/default_config.yml'
load_config_path = config_path if os.path.exists(config_path) else default_config_path
if os.path.exists(load_config_path):
    with open(load_config_path, encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}
else:
    config = {}

team_name_start_values = config.get('team_names', {})
phase_configurations = [
    phase for phase in config.get('phases', [])
    if isinstance(phase, dict) and isinstance(phase.get('name'), str) and phase['name']
]
initial_phase_configuration = phase_configurations[0] if phase_configurations else {}
initial_phase_name = initial_phase_configuration.get('name', '')

def phase_configuration(phase_name):
    return next(
        (phase for phase in phase_configurations if phase['name'] == phase_name),
        None,
    )

def phase_has_duration(phase_name):
    phase = phase_configuration(phase_name)
    return bool(phase and phase.get('duration') not in (None, ''))

def phase_duration(phase_name):
    phase = phase_configuration(phase_name)
    if not phase or not phase_has_duration(phase_name):
        return ''
    return normalize_clock_value(phase['duration'])

def apply_phase_defaults(phase_name):
    phase = phase_configuration(phase_name) or {}
    state['phase'] = phase_name
    state['time'] = phase_duration(phase_name)
    state['clock_mode'] = 'run_time' if phase.get('run_time', False) else 'stop_time'
    state['mute_on_stop'] = phase.get('mute_on_stop', True)
    state['mute'] = phase.get('start_muted', False)
    state['clock_running'] = False

def next_phase_name():
    phase_names = [phase['name'] for phase in phase_configurations]
    try:
        next_index = phase_names.index(state['phase']) + 1
    except ValueError:
        return None
    if next_index >= len(phase_names):
        return None

    if normalize_score_value(state['left_score']) == normalize_score_value(state['right_score']):
        tied_phase_name = (phase_configuration(state['phase']) or {}).get('next_phase_if_tied')
        if tied_phase_name in phase_names:
            return tied_phase_name
    return phase_names[next_index]

os.environ.setdefault('OAUTHLIB_INSECURE_TRANSPORT', '1')

state = {
    'left_team': team_name_start_values.get('left') or 'Left',
    'left_score': '0',
    'left_pp': False,
    'left_pp_time': '',
    'left_en': False,
    'right_team': team_name_start_values.get('right') or 'Right',
    'right_score': '0',
    'right_pp': False,
    'right_pp_time': '',
    'right_en': False,
    'clock_mode': 'run_time' if initial_phase_configuration.get('run_time', False) else 'stop_time',
    'clock_running': False,
    'phase': initial_phase_name,
    'time': str(initial_phase_configuration.get('duration') or ''),
    'mute': initial_phase_configuration.get('start_muted', False),
    'mute_on_stop': initial_phase_configuration.get('mute_on_stop', True),
    'incoming_audio_db': None,
    'incoming_audio_label': 'Waiting for stream',
    'incoming_audio_active': False,
    'incoming_stream_ready': False,
}
ffmpeg_process = None
relay_ffmpeg_process = None
relay_metrics_session_id = None
process_lock = threading.Lock()
relay_metrics_lock = threading.Lock()
metrics_retention_seconds = 7 * 24 * 60 * 60
metrics_last_pruned_at = 0
metrics_database_initialized = False
metrics_database_lock = threading.Lock()
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
inactive_youtube_broadcast_statuses = {'complete', 'deleted'}
deletable_youtube_broadcast_statuses = {'created', 'ready'}
primary_font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
overlay_left_text_path = '/tmp/overlay-left.txt'
overlay_right_text_path = '/tmp/overlay-right.txt'
overlay_phase_text_path = '/tmp/overlay-phase.txt'
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

def parse_ffmpeg_time(value):
    try:
        hours, minutes, seconds = value.split(':', 2)
        return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)
    except (AttributeError, TypeError, ValueError):
        return None

def parse_ffmpeg_progress_value(progress, key):
    value = progress.get(key)
    if not value or value == 'N/A':
        return None
    try:
        if key == 'speed':
            return float(value.rstrip('x'))
        if key == 'bitrate':
            match = re.match(r'(-?\d+(?:\.\d+)?)', value)
            return float(match.group(1)) if match else None
        return float(value)
    except (ValueError, IndexError):
        return None

def initialize_metrics_database():
    global metrics_database_initialized
    with metrics_database_lock:
        if metrics_database_initialized:
            return
        ensure_app_data_dir()
        with sqlite3.connect(os.path.join(app_data_dir, 'stream-metrics.sqlite3'), timeout=5) as connection:
            connection.execute('''
                CREATE TABLE IF NOT EXISTS relay_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    session_id TEXT NOT NULL,
                    broadcast_id TEXT,
                    out_time_seconds REAL,
                    total_size_bytes INTEGER,
                    speed REAL,
                    ffmpeg_bitrate_kbps REAL,
                    send_mbps REAL
                )
            ''')
            connection.execute(
                'CREATE INDEX IF NOT EXISTS relay_metrics_session_time ON relay_metrics(session_id, timestamp)'
            )
        metrics_database_initialized = True

def store_relay_metric(session_id, progress, previous_sample):
    global metrics_last_pruned_at
    now = time.time()
    out_time_seconds = parse_ffmpeg_time(progress.get('out_time'))
    if out_time_seconds is None:
        out_time_seconds = parse_ffmpeg_progress_value(progress, 'out_time_us')
        if out_time_seconds is not None:
            out_time_seconds /= 1_000_000

    total_size = parse_ffmpeg_progress_value(progress, 'total_size')
    total_size_bytes = int(total_size) if total_size is not None else None
    speed = parse_ffmpeg_progress_value(progress, 'speed')
    bitrate_kbps = parse_ffmpeg_progress_value(progress, 'bitrate')
    send_mbps = None
    if (
        total_size_bytes is not None
        and previous_sample is not None
        and previous_sample['total_size_bytes'] is not None
        and total_size_bytes >= previous_sample['total_size_bytes']
        and now > previous_sample['timestamp']
    ):
        send_mbps = ((total_size_bytes - previous_sample['total_size_bytes']) * 8) / (
            (now - previous_sample['timestamp']) * 1_000_000
        )

    with relay_metrics_lock:
        broadcast_id = runtime_youtube_destination.get('broadcast_id')
    database_path = os.path.join(app_data_dir, 'stream-metrics.sqlite3')
    initialize_metrics_database()
    with sqlite3.connect(database_path, timeout=5) as connection:
        connection.execute(
            '''INSERT INTO relay_metrics
               (timestamp, session_id, broadcast_id, out_time_seconds, total_size_bytes, speed, ffmpeg_bitrate_kbps, send_mbps)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (now, session_id, broadcast_id, out_time_seconds, total_size_bytes, speed, bitrate_kbps, send_mbps),
        )
        if now - metrics_last_pruned_at >= 300:
            connection.execute(
                'DELETE FROM relay_metrics WHERE timestamp < ?',
                (now - metrics_retention_seconds,),
            )
            metrics_last_pruned_at = now

    return {
        'timestamp': now,
        'total_size_bytes': total_size_bytes,
    }

def watch_relay_progress(process, session_id):
    progress = {}
    previous_sample = None
    for line in process.stdout:
        key, separator, value = line.strip().partition('=')
        if not separator:
            continue
        progress[key] = value
        if key == 'progress':
            try:
                previous_sample = store_relay_metric(session_id, progress, previous_sample)
            except sqlite3.Error as e:
                print(f"Could not store relay metrics: {e}", flush=True)
            progress = {}

def current_overlay_text():
    left_flags = ' '.join(flag for flag, enabled in [('PP', state['left_pp']), ('EN', state['left_en'])] if enabled)
    right_flags = ' '.join(flag for flag, enabled in [('PP', state['right_pp']), ('EN', state['right_en'])] if enabled)
    left_team = state['left_team'] or 'Left'
    right_team = state['right_team'] or 'Right'
    left_display = f"{left_team}: {state['left_score']}" + (f" {left_flags}" if left_flags else '')
    right_display = ((f"{right_flags} " if right_flags else '') + f"{right_team}: {state['right_score']}")
    phase_text = state['phase']
    time_text = state['time']

    if not phase_has_duration(state['phase']):
        time_text = phase_text
        phase_text = ''

    return {
        'left': left_display,
        'right': right_display,
        'phase': phase_text,
        'time': time_text,
        'mute': 'MUTED' if state['mute'] else '',
    }

def write_text_file(path, text):
    directory = os.path.dirname(path) or '.'
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory, delete=False) as text_file:
        text_file.write(text)
        temp_path = text_file.name
    # FFmpeg reloads these overlay files while it is running. Replace them
    # atomically so it never sees a truncated file mid-read.
    os.replace(temp_path, path)

def overlay_signature(overlay_text):
    return (
        overlay_text['left'],
        overlay_text['right'],
        overlay_text['phase'],
        overlay_text['time'],
        overlay_text['mute'],
    )

def write_overlay_text(force=False):
    global last_overlay_signature
    overlay_text = current_overlay_text()
    current_signature = overlay_signature(overlay_text)

    if not force and current_signature == last_overlay_signature:
        return False

    write_text_file(overlay_left_text_path, overlay_text['left'])
    write_text_file(overlay_right_text_path, overlay_text['right'])
    write_text_file(overlay_phase_text_path, overlay_text['phase'])
    write_text_file(overlay_time_text_path, overlay_text['time'])
    write_text_file(overlay_mute_text_path, overlay_text['mute'])
    last_overlay_signature = current_signature
    print("Overlay text updated", flush=True)
    return True

def current_state_payload():
    payload = dict(state)
    payload['phase_has_duration'] = phase_has_duration(state['phase'])
    return payload

def normalize_score_value(value):
    try:
        return str(max(0, int(value)))
    except (TypeError, ValueError):
        return '0'

def normalize_clock_value(value):
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        if ':' in text:
            minutes_text, seconds_text = text.split(':', 1)
        else:
            minutes_text, seconds_text = text, '00'
        minutes = int(minutes_text or '0')
        seconds = int(seconds_text or '0')
        if minutes < 0 or seconds < 0 or seconds > 59:
            raise ValueError
        return f"{minutes}:{seconds:02d}"
    except (TypeError, ValueError):
        return text

def parse_clock(clock_text):
    normalized_clock = normalize_clock_value(clock_text)
    if not normalized_clock:
        return None
    try:
        minutes_text, seconds_text = normalized_clock.split(':', 1)
        return (int(minutes_text) * 60) + int(seconds_text)
    except (ValueError, AttributeError):
        return None

def format_clock(total_seconds):
    total_seconds = max(0, int(total_seconds))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"

def adjust_pp_times(seconds_delta):
    for team in ('left', 'right'):
        if not state[f'{team}_pp']:
            continue
        remaining_seconds = parse_clock(state[f'{team}_pp_time'])
        if remaining_seconds is None:
            continue
        adjusted_seconds = max(0, remaining_seconds + seconds_delta)
        if adjusted_seconds == 0:
            state[f'{team}_pp_time'] = ''
            state[f'{team}_pp'] = False
        else:
            state[f'{team}_pp_time'] = format_clock(adjusted_seconds)

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

def sync_mute_state(previous_mute):
    if state['mute'] == previous_mute:
        return True

    with process_lock:
        if is_ffmpeg_running() and ffmpeg_ready:
            if update_live_volume():
                return True
            print("Live volume update failed; reverting mute state", flush=True)
            with state_lock:
                state['mute'] = previous_mute
            return False

        print("FFmpeg not ready; mute state will apply on next FFmpeg start", flush=True)
        return True

def tick_game_clock():
    while True:
        time.sleep(1)
        previous_mute = None
        with state_lock:
            if not state['clock_running']:
                continue
            previous_mute = state['mute']
            remaining_seconds = parse_clock(state['time'])
            if remaining_seconds is None:
                continue
            if remaining_seconds <= 0:
                state['clock_running'] = False
                next_phase = next_phase_name() if phase_has_duration(state['phase']) else None
                if next_phase:
                    apply_phase_defaults(next_phase)
                elif state['clock_mode'] == 'stop_time' and state['mute_on_stop']:
                    state['mute'] = True
                updated_state = current_state_payload()
            else:
                state['time'] = format_clock(remaining_seconds - 1)
                adjust_pp_times(-1)
                if parse_clock(state['time']) == 0:
                    state['clock_running'] = False
                    next_phase = next_phase_name() if phase_has_duration(state['phase']) else None
                    if next_phase:
                        apply_phase_defaults(next_phase)
                    elif state['clock_mode'] == 'stop_time' and state['mute_on_stop']:
                        state['mute'] = True
                updated_state = current_state_payload()
            sync_mute_state(previous_mute)
        write_overlay_text()
        socketio.emit('state_updated', current_state_payload())

def current_input_url():
    return 'rtmp://mediamtx/live/egl'

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
        if runtime_youtube_destination.get('broadcast_status') in inactive_youtube_broadcast_statuses:
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
            yaml.safe_dump(config, f, sort_keys=False)

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

def youtube_title_for_today(left_team=None, right_team=None):
    if left_team is None or right_team is None:
        with state_lock:
            left_team = left_team or state.get('left_team') or 'Left'
            right_team = right_team or state.get('right_team') or 'Right'
    title_timezone = ZoneInfo(os.getenv('TZ'))
    return f"{left_team} vs {right_team} - {datetime.now(title_timezone).date().isoformat()}"

def youtube_status_snapshot():
    active_destination = (
        dict(runtime_youtube_destination)
        if runtime_youtube_destination.get('broadcast_id') and runtime_youtube_destination.get('broadcast_status') not in inactive_youtube_broadcast_statuses
        else {}
    )
    active_destination.pop('stream_key', None)
    return {
        'authorized': os.path.exists(google_token_path),
        'oauth_configured': google_client_config() is not None,
        'channel_choices': [],
        'active_destination': active_destination,
        'can_stop': bool(runtime_youtube_destination.get('broadcast_id') and runtime_youtube_destination.get('broadcast_status') not in inactive_youtube_broadcast_statuses),
        'configured_stream_key': bool(os.getenv('YOUTUBE_STREAM_KEY') or config.get('youtube_stream_key')),
    }

def active_youtube_broadcast_runtime_status(service):
    broadcast_id = runtime_youtube_destination.get('broadcast_id')
    if not broadcast_id or runtime_youtube_destination.get('broadcast_status') in inactive_youtube_broadcast_statuses:
        return {}

    broadcast_response = service.liveBroadcasts().list(
        part='status',
        id=broadcast_id,
    ).execute()
    broadcast_items = broadcast_response.get('items') or []
    if not broadcast_items:
        return {
            'life_cycle_status': 'missing',
            'is_live': False,
            'concurrent_viewers': None,
        }

    life_cycle_status = ((broadcast_items[0].get('status') or {}).get('lifeCycleStatus')) or runtime_youtube_destination.get('broadcast_status')

    video_response = service.videos().list(
        part='liveStreamingDetails,status',
        id=broadcast_id,
    ).execute()
    video_items = video_response.get('items') or []
    live_streaming_details = (video_items[0].get('liveStreamingDetails') or {}) if video_items else {}
    video_status = (video_items[0].get('status') or {}) if video_items else {}
    concurrent_viewers = live_streaming_details.get('concurrentViewers')

    stream_status = {}
    stream_id = runtime_youtube_destination.get('stream_id')
    if stream_id:
        stream_response = service.liveStreams().list(
            part='status',
            id=stream_id,
        ).execute()
        stream_items = stream_response.get('items') or []
        stream_status = (stream_items[0].get('status') or {}) if stream_items else {}

    try:
        concurrent_viewers = int(concurrent_viewers) if concurrent_viewers is not None else None
    except (TypeError, ValueError):
        concurrent_viewers = None

    runtime_youtube_destination['broadcast_status'] = life_cycle_status
    save_runtime_youtube_destination()

    return {
        'life_cycle_status': life_cycle_status,
        'is_live': life_cycle_status == 'live',
        'concurrent_viewers': concurrent_viewers,
        'public_stats_viewable': video_status.get('publicStatsViewable'),
        'stream_status': stream_status,
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

def create_youtube_broadcast_for_channel(channel_id, left_team=None, right_team=None):
    service = youtube_service()
    channels = youtube_channel_choices(service)
    selected_channel = next((item for item in channels if item['id'] == channel_id), None)
    if selected_channel is None:
        raise ValueError('Selected YouTube channel is no longer available')

    now = datetime.now(timezone.utc).replace(microsecond=0)
    title = youtube_title_for_today(left_team=left_team, right_team=right_team)
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

def update_active_youtube_broadcast_title(left_team=None, right_team=None):
    broadcast_id = runtime_youtube_destination.get('broadcast_id')
    if not broadcast_id or runtime_youtube_destination.get('broadcast_status') in inactive_youtube_broadcast_statuses:
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
    title = youtube_title_for_today(left_team=left_team, right_team=right_team)
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

def stop_relay_ffmpeg():
    global relay_ffmpeg_process
    with process_lock:
        if relay_ffmpeg_process is not None:
            stop_process(relay_ffmpeg_process)
            relay_ffmpeg_process = None

def current_youtube_broadcast_status(service, broadcast_id):
    response = service.liveBroadcasts().list(
        part='status',
        id=broadcast_id,
    ).execute()
    items = response.get('items') or []
    if not items:
        return 'deleted'
    return ((items[0].get('status') or {}).get('lifeCycleStatus')) or runtime_youtube_destination.get('broadcast_status')

def stop_active_youtube_broadcast():
    broadcast_id = runtime_youtube_destination.get('broadcast_id')
    if not broadcast_id:
        raise ValueError('No active YouTube broadcast is available to stop')
    if runtime_youtube_destination.get('broadcast_status') in inactive_youtube_broadcast_statuses:
        return dict(runtime_youtube_destination)

    service = youtube_service()
    current_status = current_youtube_broadcast_status(service, broadcast_id)

    if current_status in deletable_youtube_broadcast_statuses:
        service.liveBroadcasts().delete(
            id=broadcast_id,
        ).execute()
        runtime_youtube_destination['broadcast_status'] = 'deleted'
        save_runtime_youtube_destination()
        stop_relay_ffmpeg()
        return dict(runtime_youtube_destination)

    if current_status in inactive_youtube_broadcast_statuses:
        runtime_youtube_destination['broadcast_status'] = current_status
        save_runtime_youtube_destination()
        stop_relay_ffmpeg()
        return dict(runtime_youtube_destination)

    transitioned = service.liveBroadcasts().transition(
        part='status',
        id=broadcast_id,
        broadcastStatus='complete',
    ).execute()

    runtime_youtube_destination['broadcast_status'] = ((transitioned.get('status') or {}).get('lifeCycleStatus')) or 'complete'
    save_runtime_youtube_destination()
    stop_relay_ffmpeg()

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
    global relay_ffmpeg_process, relay_metrics_session_id
    upstream_url = current_upstream_output_url()
    if not upstream_url:
        return
    if is_relay_ffmpeg_running():
        return

    input_url = current_preview_output_url()
    if not mediamtx_path_ready(stream_path_from_url(input_url)):
        return

    session_id = uuid.uuid4().hex
    try:
        relay_cmd = [
            'ffmpeg',
            '-rtmp_live', 'live',
            '-i', input_url,
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-f', 'flv',
            '-stats_period', '1',
            '-progress', 'pipe:1',
            upstream_url,
        ]
        print("Starting relay FFmpeg:", ' '.join(relay_cmd), flush=True)
        relay_ffmpeg_process = subprocess.Popen(
            relay_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        with relay_metrics_lock:
            relay_metrics_session_id = session_id
        threading.Thread(target=watch_relay_ffmpeg, args=(relay_ffmpeg_process,), daemon=True).start()
        threading.Thread(
            target=watch_relay_progress,
            args=(relay_ffmpeg_process, session_id),
            daemon=True,
        ).start()
    except Exception as e:
        print(f"Error starting relay FFmpeg: {e}", flush=True)
        relay_ffmpeg_process = None
        with relay_metrics_lock:
            relay_metrics_session_id = None

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
            f"[0:v]drawtext=fontfile={primary_font_path}:textfile={overlay_left_text_path}:reload=1:fontcolor=white:fontsize=38:box=1:boxcolor=black@0.69:boxborderw=18:x=18:y=18[v_left]",
            f"[v_left]drawtext=fontfile={primary_font_path}:textfile={overlay_right_text_path}:reload=1:fontcolor=white:fontsize=38:box=1:boxcolor=black@0.69:boxborderw=18:x=w-tw-18:y=18[v_right]",
            f"[v_right]drawtext=fontfile={primary_font_path}:textfile={overlay_time_text_path}:reload=1:fontcolor=white:fontsize=44:box=1:boxcolor=black@0.69:boxborderw=18:x=(w-tw)/2:y=18[v_time]",
            f"[v_time]drawtext=fontfile={primary_font_path}:textfile={overlay_phase_text_path}:reload=1:fontcolor=white:fontsize=32:box=1:boxcolor=black@0.69:boxborderw=18:x=18:y=h-th-18[v_phase]",
            f"[v_phase]drawtext=fontfile={primary_font_path}:textfile={overlay_mute_text_path}:reload=1:fontcolor=white@0.76:fontsize=38:x=w-tw-34:y=h-th-72[v_composited]",
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
    last_ready = None
    while True:
        ready = is_stream_ready()
        if ready != last_ready:
            with state_lock:
                state['incoming_stream_ready'] = ready
            socketio.emit('state_updated', current_state_payload())
            last_ready = ready
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
        clock_adjustment_seconds = data.pop('clock_adjustment_seconds', 0)
        try:
            clock_adjustment_seconds = int(clock_adjustment_seconds)
        except (TypeError, ValueError):
            clock_adjustment_seconds = 0
        if clock_adjustment_seconds not in (-1, 0, 1):
            clock_adjustment_seconds = 0
        previous_mute = state['mute']
        previous_time = state['time']
        previous_phase = state['phase']
        phase_changed = 'phase' in data and phase_configuration(data['phase']) is not None and data['phase'] != previous_phase
        previous_left_team = state['left_team']
        previous_right_team = state['right_team']
        if 'phase' in data and phase_configuration(data['phase']) is None:
            data.pop('phase')
        if 'left_score' in data:
            data['left_score'] = normalize_score_value(data['left_score'])
        if 'right_score' in data:
            data['right_score'] = normalize_score_value(data['right_score'])
        if 'time' in data:
            data['time'] = normalize_clock_value(data['time'])
        for key in ('left_pp_time', 'right_pp_time'):
            if key in data:
                data[key] = normalize_clock_value(data[key])
        if 'clock_mode' in data and data['clock_mode'] not in ('stop_time', 'run_time'):
            data['clock_mode'] = 'stop_time'
        state.update(data)
        if phase_changed:
            apply_phase_defaults(state['phase'])
        if clock_adjustment_seconds:
            adjust_pp_times(clock_adjustment_seconds)
        # The UI posts the full form for mute/unmute actions, so `time` can be
        # present even when the operator did not edit the clock. Also, changing
        # clock mode or phase should not stop or start the clock by itself.
        # Only stop a running run-time clock when the time value changed.
        if state['clock_mode'] == 'run_time' and (
            'time' in data and data['time'] != previous_time
        ):
            state['clock_running'] = False
        if phase_has_duration(state['phase']) and not phase_changed and 'mute_on_stop' in data and state['clock_mode'] == 'stop_time' and state['mute_on_stop'] and not state['clock_running']:
            state['mute'] = True
        if 'clock_running' in data:
            state['clock_running'] = bool(data['clock_running'])
            if phase_has_duration(state['phase']) and state['clock_mode'] == 'stop_time' and state['mute_on_stop']:
                state['mute'] = not state['clock_running']
        updated_state = current_state_payload()
    print("Updating overlay with data:", data, flush=True)
    if updated_state['mute'] != previous_mute:
        sync_mute_state(previous_mute)
    if (
        runtime_youtube_destination.get('broadcast_id')
        and runtime_youtube_destination.get('broadcast_status') not in inactive_youtube_broadcast_statuses
        and (
            updated_state['left_team'] != previous_left_team
            or updated_state['right_team'] != previous_right_team
        )
    ):
        try:
            updated_title = update_active_youtube_broadcast_title(
                left_team=updated_state['left_team'],
                right_team=updated_state['right_team'],
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
    toggle_buffer_ms = config.get('toggle_buffer_ms', 400)
    return render_template(
        'index.html',
        webrtc_additional_hosts=os.getenv('MTX_WEBRTCADDITIONALHOSTS', ''),
        toggle_buffer_ms=toggle_buffer_ms,
        phases=phase_configurations,
        initial_phase=initial_phase_name,
    )

@app.route('/youtube-stop')
def youtube_stop_page():
    return render_template('youtube-stop.html')

@app.route('/api/state', methods=['GET'])
def get_state():
    return jsonify(current_state_payload())

@app.route('/api/state', methods=['POST'])
def update_state():
    data = request.get_json(silent=True) or request.form.to_dict()
    if 'mute' in data and not isinstance(data['mute'], bool):
        data['mute'] = str(data['mute']).lower() in ('1', 'true', 'yes', 'on')
    for key in ('left_pp', 'left_en', 'right_pp', 'right_en', 'mute_on_stop'):
        if key in data and not isinstance(data[key], bool):
            data[key] = str(data[key]).lower() in ('1', 'true', 'yes', 'on')
    if 'clock_running' in data and not isinstance(data['clock_running'], bool):
        data['clock_running'] = str(data['clock_running']).lower() in ('1', 'true', 'yes', 'on')
    updated_state = apply_overlay_update(data)
    return jsonify(updated_state)

@app.route('/api/stream/metrics', methods=['GET'])
def stream_metrics():
    duration_text = request.args.get('minutes', '15')
    if not re.fullmatch(r'[0-9]+', duration_text):
        return jsonify({'error': 'minutes must be an integer'}), 400
    duration_minutes = int(duration_text)
    if not 1 <= duration_minutes <= 1440:
        return jsonify({'error': 'minutes must be between 1 and 1440'}), 400

    with relay_metrics_lock:
        session_id = relay_metrics_session_id

    try:
        initialize_metrics_database()
        cutoff = time.time() - (duration_minutes * 60)
        with sqlite3.connect(os.path.join(app_data_dir, 'stream-metrics.sqlite3'), timeout=5) as connection:
            if not session_id:
                latest_session = connection.execute(
                    '''SELECT session_id FROM relay_metrics
                       WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT 1''',
                    (cutoff,),
                ).fetchone()
                session_id = latest_session[0] if latest_session else None
            if not session_id:
                return jsonify({'session_id': None, 'samples': []})
            rows = connection.execute(
                '''SELECT timestamp, out_time_seconds, total_size_bytes, speed,
                          ffmpeg_bitrate_kbps, send_mbps
                   FROM relay_metrics
                   WHERE session_id = ? AND timestamp >= ?
                   ORDER BY timestamp''',
                (session_id, cutoff),
            ).fetchall()
    except sqlite3.Error as e:
        return jsonify({'session_id': session_id, 'samples': [], 'error': str(e)}), 503

    samples = [
        {
            'timestamp': row[0],
            'out_time_seconds': row[1],
            'total_size_bytes': row[2],
            'speed': row[3],
            'ffmpeg_bitrate_kbps': row[4],
            'send_mbps': row[5],
        }
        for row in rows
    ]
    if len(samples) > 301:
        step = (len(samples) - 1) / 300
        samples = [samples[round(index * step)] for index in range(301)]
    return jsonify({'session_id': session_id, 'samples': samples})

@app.route('/api/youtube/status', methods=['GET'])
def youtube_status():
    snapshot = youtube_status_snapshot()
    try:
        service = youtube_service()
        snapshot['authorized'] = True
        snapshot['channel_choices'] = youtube_channel_choices(service)
        if snapshot['active_destination']:
            snapshot['active_destination'].update(active_youtube_broadcast_runtime_status(service))
    except Exception as e:
        snapshot['authorized'] = False
        snapshot['authorization_error'] = str(e)
    return jsonify(snapshot)

@app.route('/api/youtube/oauth/start', methods=['GET'])
def youtube_oauth_start():
    client_config = google_client_config()
    if client_config is None:
        return jsonify({
            'error': 'Missing Google OAuth client configuration. Add google_oauth_client_secret.json to config/ or set google_oauth_client_id/google_oauth_client_secret in config.yml.'
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

    left_team = (data.get('left_team') or '').strip() or None
    right_team = (data.get('right_team') or '').strip() or None

    try:
        created = create_youtube_broadcast_for_channel(
            channel_id,
            left_team=left_team,
            right_team=right_team,
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
