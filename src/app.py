import os
import subprocess
import json
import re
import sqlite3
import threading
import wave
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
from PIL import ImageFont
from flask import Flask, Response, render_template, request, jsonify, redirect, session, url_for
from flask_socketio import SocketIO, emit
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from penalties import PenaltyStateMachine, PenaltyType, Team
from scoresheets import GameNotStarted, GoalChangeKind, ManualGoals, ScoresheetError, is_mock_game, open_feed, player_text
from goal_captions import Teams

# Load config
config_filename = os.getenv('APP_CONFIG_FILE') or 'config.yml'
config_path = os.path.join('/app/config', config_filename)
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

goal_teams = Teams(config.get('teams'))
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
    'message_text': '',
    'message_show': False,
    'message_bold': True,
    'message_centered': True,
    'message_background': True,
    'goal_auto_show': bool(config.get('goal_auto_show', True)),
}
# Each side's Celebrate checkbox starts from the config for its startup team
# name, then is only changed in the UI, not by renaming the team.
state['left_celebrate'] = goal_teams.celebrate(state['left_team'])
state['right_celebrate'] = goal_teams.celebrate(state['right_team'])
celebrate_positions = ('top', 'middle', 'bottom')
state['celebrate_position'] = str(config.get('celebrate_position') or 'top').lower()
if state['celebrate_position'] not in celebrate_positions:
    state['celebrate_position'] = 'top'
penalty_game = PenaltyStateMachine()
# Scoresheet goals, for the UI's goal list and the goal caption on the
# overlay. Guarded by state_lock. Holds players' names, so don't save or log
# it.
scoresheet_poll_seconds = float(config.get('scoresheet_poll_seconds', 5))
# Right after a new goal, check often so the assists show up quickly.
scoresheet_new_goal_poll_seconds = 1
scoresheet_new_goal_fast_seconds = 15
scoresheet_finished_poll_seconds = 60
goal_overlay_seconds = float(config.get('goal_overlay_seconds', 10))
scoresheet = {
    'goals': [],
    'settled': set(),
    'game_over': False,
    'error': '',
    # The scoresheet doesn't exist yet. Not an error: polling carries on.
    'not_started': False,
    'checked_at': None,
    'showing': None,
    'showing_until': 0.0,
    'queue': [],
    # Goals already queued automatically, so each is only auto-shown once.
    'auto_shown': set(),
    # Poll every scoresheet_new_goal_poll_seconds until then (monotonic).
    'fast_poll_until': 0.0,
}
# Goals entered in the UI, for when the scoresheet isn't being updated (an
# arena with no WiFi). Listed and captioned like scoresheet goals; a
# scoresheet goal that turns up later replaces its manual goal. Guarded by
# state_lock. Holds players' names too, so don't save or log it.
manual_goals = ManualGoals()
# The goal-entry form's defaults for each side (period, time, strength, EN),
# taken when its score "+" is pressed, as the penalty and EN state change
# right after. None means use the current game state.
manual_goal_defaults = {'left': None, 'right': None}
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
# Probed from the incoming stream just before the overlay FFmpeg starts; None
# when there is no stream. Overlay sizes scale with it (see overlay_layout).
incoming_video_resolution = None
incoming_video_frame_rate = None
default_video_resolution = '1280x720'
overlay_preview_background = '0x1b262d'
mediamtx_api_url = config.get('mediamtx_api_url', 'http://mediamtx:9997/v3/paths/list')
last_stream_ready_state = None
audio_control_target = 'volume@audio_gain'
# A goal for a team with Celebrate on plays this file into the program audio.
# It's looked for at each goal, so it can be added or changed at any time.
celebrate_audio_path = '/app/config/celebrate.m4a'
# FFmpeg can't open a new source while running, so its audio graph always reads
# this buffer, and each goal decodes the file into it.
celebrate_audio_buffer_path = '/tmp/celebrate-audio.wav'
celebrate_audio_buffer_seconds = 60
celebrate_audio_rate = 48000
celebrate_audio_channels = 2
celebrate_audio_sample_bytes = 2
celebrate_audio_bytes_per_second = celebrate_audio_rate * celebrate_audio_channels * celebrate_audio_sample_bytes
# Silence around the clip, so the time taken by the stdin commands that turn
# the audio up and down doesn't cut it.
celebrate_audio_lead_seconds = 0.2
celebrate_audio_tail_seconds = 0.5
celebrate_audio_source = 'amovie@celebrate_audio'
celebrate_audio_gain = 'volume@celebrate_gain'
celebrate_audio_lock = threading.Lock()
celebrate_audio_buffer_data_offset = 0
celebrate_text_seconds = 12
celebrate_text = ''
# Bumped by each celebration, so a newer one keeps its text and audio when an
# older one's timers run out.
celebrate_generation = 0
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
# What "Create New Stream" declares to YouTube. The relay copies the video
# as-is, so this isn't necessarily what is actually sent.
youtube_stream_resolution = '720p'
youtube_stream_frame_rate = '30fps'
inactive_youtube_broadcast_statuses = {'complete', 'deleted'}
deletable_youtube_broadcast_statuses = {'created', 'ready'}
primary_font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
regular_font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
overlay_left_text_path = '/tmp/overlay-left.txt'
overlay_right_text_path = '/tmp/overlay-right.txt'
overlay_phase_text_path = '/tmp/overlay-phase.txt'
overlay_time_text_path = '/tmp/overlay-time.txt'
overlay_mute_text_path = '/tmp/overlay-mute.txt'
overlay_left_penalty_text_path = '/tmp/overlay-left-penalty.txt'
overlay_right_penalty_text_path = '/tmp/overlay-right-penalty.txt'
overlay_message_text_path = '/tmp/overlay-message.txt'
overlay_message_bold_text_path = '/tmp/overlay-message-bold.txt'
overlay_celebrate_text_path = '/tmp/overlay-celebrate.txt'
overlay_goal_text_path = '/tmp/overlay-goal.txt'
last_overlay_signature = None
# Overlay sizes in pixels at 1280x720. overlay_layout() scales them to the
# actual video so the overlay is the same size relative to the frame.
overlay_base_layout = {
    'margin': 18,
    'box_border': 18,
    'team_font': 38,
    'time_font': 44,
    'small_font': 32,
    'message_font': 44,
    'message_line_spacing': 10,
    'message_outline': 3,
    'goal_font': 30,
}
overlay_penalty_filters = {'left': 'drawtext@left_penalty', 'right': 'drawtext@right_penalty'}
sent_penalty_x_expressions = {}
# fontfile can't change at runtime, so the message has a regular and a bold
# drawtext stage and only the selected one gets text. Alignment and background
# can change, so they are sent over stdin (see message_filter_options).
overlay_message_filters = ('drawtext@message', 'drawtext@message_bold')
sent_message_options = {}
overlay_celebrate_filter = 'drawtext@celebrate'
# The goal caption is shown where the Caption setting puts the celebrate text.
overlay_goal_filter = 'drawtext@goal'
sent_celebrate_y = None
overlay_fonts = {}
ffmpeg_stdin_lock = threading.Lock()

def ensure_app_data_dir():
    os.makedirs(app_data_dir, exist_ok=True)

def cleanup_ffmpeg():
    global ffmpeg_process, ffmpeg_ready
    ffmpeg_process = None
    ffmpeg_ready = False
    set_incoming_video_format(None, None)

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

def set_incoming_video_format(resolution, frame_rate):
    global incoming_video_resolution, incoming_video_frame_rate
    if (resolution, frame_rate) == (incoming_video_resolution, incoming_video_frame_rate):
        return
    incoming_video_resolution = resolution
    incoming_video_frame_rate = frame_rate
    print(f"Incoming video: {resolution or 'none'} at {frame_rate or 'unknown'} fps", flush=True)
    socketio.emit('state_updated', current_state_payload())

def format_frame_rate(rate):
    # ffprobe gives a fraction such as "30/1" or "30000/1001".
    match = re.fullmatch(r'(\d+)/(\d+)', rate or '')
    if not match or int(match.group(2)) == 0:
        return None
    return f'{int(match.group(1)) / int(match.group(2)):.2f}'.rstrip('0').rstrip('.')

def resolution_label(resolution):
    # "1280x720" -> "720p". Uses the shorter side, so a portrait stream
    # (720x1280) is also "720p".
    if not resolution:
        return None
    return f"{min(int(value) for value in resolution.split('x'))}p"

def probe_video_format(url):
    # Returns (resolution, frame rate), or (None, None) if the probe fails.
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height,r_frame_rate', '-of', 'json', url],
            capture_output=True,
            text=True,
            timeout=15,
        )
        stream = (json.loads(result.stdout or '{}').get('streams') or [{}])[0]
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        print(f"Could not probe incoming video: {e}", flush=True)
        return None, None
    if not stream.get('width') or not stream.get('height'):
        print(f"Could not probe incoming video: {result.stderr.strip()}", flush=True)
        return None, None
    return f"{stream['width']}x{stream['height']}", format_frame_rate(stream.get('r_frame_rate'))

def overlay_layout(resolution=None):
    width, height = (int(value) for value in (resolution or default_video_resolution).split('x'))
    scale = min(width / 1280, height / 720)
    return {key: max(1, round(value * scale)) for key, value in overlay_base_layout.items()}

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
    socketio.emit('state_updated', current_state_payload())

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
    left_team = state['left_team'] or 'Left'
    right_team = state['right_team'] or 'Right'
    left_display = f"{left_team}: {state['left_score']}" + (" PP" if state['left_pp'] else '')
    right_display = ("PP " if state['right_pp'] else '') + f"{right_team}: {state['right_score']}"
    penalty_text = current_penalty_overlay_text()
    phase_text = state['phase']
    time_text = state['time']
    message_text = state['message_text'] if state['message_show'] else ''

    if not phase_has_duration(state['phase']):
        time_text = phase_text
        phase_text = ''
    else:
        time_text = format_overlay_clock(time_text)

    goal_caption = current_goal_caption()
    return {
        'left': left_display,
        'right': right_display,
        'left_penalty': penalty_text['left'],
        'right_penalty': penalty_text['right'],
        'phase': phase_text,
        'time': time_text,
        'mute': 'MUTED' if state['mute'] else '',
        # The goal caption is in the same place and says who scored, so it
        # replaces the "<Team> Goal!" text.
        'celebrate': '' if goal_caption else celebrate_text,
        'message': message_text if not state['message_bold'] else '',
        'message_bold': message_text if state['message_bold'] else '',
        'goal': goal_caption,
    }

def scoresheet_team_name(goal, side_teams):
    if is_manual_goal(goal):
        return goal.team_name
    return side_teams.get(goal.team) or goal.team.capitalize()

def current_goal_caption():
    goal = scoresheet_goal(scoresheet['showing'])
    if goal is None:
        return ''
    side_teams = scoresheet_side_teams()
    return goal_teams.caption(goal, scoresheet_team_name(goal, side_teams), settled=goal_is_settled(goal.id))

def scoresheet_goal(goal_id):
    # A scoresheet goal or a manual one.
    manual = manual_goals.get(goal_id) if goal_id is not None else None
    return manual or next((goal for goal in scoresheet['goals'] if goal.id == goal_id), None)

def is_manual_goal(goal):
    return manual_goals.get(goal.id) is goal

def goal_is_settled(goal_id):
    # A manual goal is finished as soon as it's entered.
    return goal_id in scoresheet['settled'] or manual_goals.get(goal_id) is not None

def format_overlay_clock(value):
    total_seconds = parse_clock(value)
    if total_seconds is None:
        return value
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"

def current_penalty_overlay():
    if penalty_game.strength == f"{penalty_game.base_skaters}-on-{penalty_game.base_skaters}":
        return {'side': None, 'label': '', 'time': ''}
    next_change = penalty_game.next_manpower_change
    if next_change is None:
        return {'side': None, 'label': '', 'time': ''}
    home_skaters = penalty_game.skaters_for(Team.HOME)
    away_skaters = penalty_game.skaters_for(Team.AWAY)
    if max(home_skaters, away_skaters) == penalty_game.base_skaters:
        advantage = abs(home_skaters - away_skaters)
        label = 'PP' if advantage == 1 else f'{advantage}+PP'
    else:
        label = penalty_game.strength
    next_home, next_away, remaining_seconds = next_change
    if home_skaters != away_skaters:
        display_team = Team.HOME if home_skaters > away_skaters else Team.AWAY
    elif state['left_en'] != state['right_en']:
        # EN goes on the pulled-goalie team's side, so show the even
        # strength on the other side.
        display_team = Team.AWAY if state['left_en'] else Team.HOME
    elif next_home != next_away:
        display_team = Team.HOME if next_home < next_away else Team.AWAY
    else:
        display_team = Team.HOME
    return {
        'side': 'left' if display_team is Team.HOME else 'right',
        'label': label,
        'time': format_clock(remaining_seconds),
    }

def current_penalty_overlay_text():
    # Each side's penalty box combines the manpower state and EN. EN sits
    # toward the centre: "2:00 PP - EN" on the left, "EN - PP 2:00" on the right.
    penalty_overlay = current_penalty_overlay()
    texts = {}
    for side in ('left', 'right'):
        parts = []
        if penalty_overlay['side'] == side:
            if side == 'left':
                parts.append(f"{penalty_overlay['time']} {penalty_overlay['label']}")
            else:
                parts.append(f"{penalty_overlay['label']} {penalty_overlay['time']}")
        if state[f'{side}_en']:
            parts.append('EN')
        if side == 'right':
            parts.reverse()
        texts[side] = ' - '.join(parts)
    return texts

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
        overlay_text['celebrate'],
        overlay_text['left_penalty'],
        overlay_text['right_penalty'],
        overlay_text['phase'],
        overlay_text['time'],
        overlay_text['mute'],
        overlay_text['message'],
        overlay_text['message_bold'],
        overlay_text['goal'],
    )

def overlay_text_width(text, font_size):
    if font_size not in overlay_fonts:
        overlay_fonts[font_size] = ImageFont.truetype(primary_font_path, font_size)
    return overlay_fonts[font_size].getlength(text)

def penalty_x_expressions(overlay_text, layout):
    # Centre each penalty box in the gap between its team's box and the
    # centred time box. drawtext boxes extend the same border on every side,
    # so the borders cancel out of the midpoint. `tw` is the penalty text's
    # own width, which FFmpeg evaluates per frame.
    left_width = round(overlay_text_width(overlay_text['left'], layout['team_font']))
    right_width = round(overlay_text_width(overlay_text['right'], layout['team_font']))
    time_width = round(overlay_text_width(overlay_text['time'], layout['time_font']))
    return {
        'left': f"({layout['margin'] + left_width}+(w-{time_width})/2)/2-tw/2",
        'right': f"(w-{layout['margin'] + right_width}+(w+{time_width})/2)/2-tw/2",
    }

def sync_penalty_overlay_positions(overlay_text):
    if not is_ffmpeg_running() or not ffmpeg_ready:
        return
    for side, expression in penalty_x_expressions(overlay_text, overlay_layout(incoming_video_resolution)).items():
        if sent_penalty_x_expressions.get(side) == expression:
            continue
        if send_ffmpeg_stdin_command(f'{overlay_penalty_filters[side]} -1 x {expression}'):
            sent_penalty_x_expressions[side] = expression

def message_filter_options(layout):
    # Without a background the text gets a black outline instead, so it
    # stays readable over the video.
    background = state['message_background']
    return {
        'text_align': 'center' if state['message_centered'] else 'left',
        'box': '1' if background else '0',
        'borderw': str(0 if background else layout['message_outline']),
    }

def sync_message_options():
    if not is_ffmpeg_running() or not ffmpeg_ready:
        return
    for option, value in message_filter_options(overlay_layout(incoming_video_resolution)).items():
        if sent_message_options.get(option) == value:
            continue
        if all(send_ffmpeg_stdin_command(f'{target} -1 {option} {value}') for target in overlay_message_filters):
            sent_message_options[option] = value

def celebrate_y_expression(layout):
    # Top sits just under the top row and Bottom just over the bottom row;
    # Middle shares the centre with the message.
    position = state['celebrate_position']
    if position == 'middle':
        return '(h-th)/2'
    if position == 'bottom':
        return f"h-th-{layout['margin'] + layout['small_font'] + 3 * layout['box_border']}"
    return str(layout['margin'] + layout['time_font'] + 3 * layout['box_border'])

def sync_celebrate_position():
    global sent_celebrate_y
    if not is_ffmpeg_running() or not ffmpeg_ready:
        return
    expression = celebrate_y_expression(overlay_layout(incoming_video_resolution))
    if sent_celebrate_y == expression:
        return
    if all(send_ffmpeg_stdin_command(f'{target} -1 y {expression}')
           for target in (overlay_celebrate_filter, overlay_goal_filter)):
        sent_celebrate_y = expression

def write_overlay_text(force=False):
    global last_overlay_signature
    overlay_text = current_overlay_text()
    current_signature = overlay_signature(overlay_text)
    sync_penalty_overlay_positions(overlay_text)
    sync_message_options()
    sync_celebrate_position()

    if not force and current_signature == last_overlay_signature:
        return False

    write_text_file(overlay_left_text_path, overlay_text['left'])
    write_text_file(overlay_right_text_path, overlay_text['right'])
    write_text_file(overlay_left_penalty_text_path, overlay_text['left_penalty'])
    write_text_file(overlay_right_penalty_text_path, overlay_text['right_penalty'])
    write_text_file(overlay_phase_text_path, overlay_text['phase'])
    write_text_file(overlay_time_text_path, overlay_text['time'])
    write_text_file(overlay_mute_text_path, overlay_text['mute'])
    write_text_file(overlay_celebrate_text_path, overlay_text['celebrate'])
    write_text_file(overlay_message_text_path, overlay_text['message'])
    write_text_file(overlay_message_bold_text_path, overlay_text['message_bold'])
    write_text_file(overlay_goal_text_path, overlay_text['goal'])
    last_overlay_signature = current_signature
    print("Overlay text updated", flush=True)
    return True

def current_state_payload():
    payload = dict(state)
    payload['left_pp'] = state['left_pp']
    payload['right_pp'] = state['right_pp']
    payload['manpower'] = penalty_game.strength
    payload['penalty_overlay'] = current_penalty_overlay()
    payload['penalty_overlay_text'] = current_penalty_overlay_text()
    payload['penalties'] = [
        {
            'id': penalty.id,
            'team': penalty.team.value,
            'player': penalty.player,
            'kind': penalty.kind.value,
            'remaining_seconds': penalty.remaining_seconds,
            'status': penalty.status.value,
            'segments_remaining': penalty.segments_remaining,
        }
        for penalty in penalty_game.active_penalties()
    ]
    payload['phase_has_duration'] = phase_has_duration(state['phase'])
    payload['scoresheet'] = current_scoresheet_payload()
    relay_running = is_relay_ffmpeg_running()
    payload['stream_quality'] = {
        'incoming': {'resolution': resolution_label(incoming_video_resolution), 'fps': incoming_video_frame_rate},
        'outgoing': {
            'resolution': youtube_stream_resolution if relay_running else None,
            'fps': youtube_stream_frame_rate.replace('fps', '') if relay_running else None,
        },
    }
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

def silent_audio(seconds):
    return bytes(int(seconds * celebrate_audio_bytes_per_second))

def create_celebrate_audio_buffer():
    global celebrate_audio_buffer_data_offset
    with wave.open(celebrate_audio_buffer_path, 'wb') as buffer:
        buffer.setnchannels(celebrate_audio_channels)
        buffer.setsampwidth(celebrate_audio_sample_bytes)
        buffer.setframerate(celebrate_audio_rate)
        buffer.writeframes(silent_audio(celebrate_audio_buffer_seconds))
    data_bytes = int(celebrate_audio_buffer_seconds * celebrate_audio_bytes_per_second)
    celebrate_audio_buffer_data_offset = os.path.getsize(celebrate_audio_buffer_path) - data_bytes

def load_celebrate_audio():
    # Decodes the file into the buffer FFmpeg is reading, in place so that
    # FFmpeg sees it. Returns the seconds to play, or None if there's no audio.
    if not os.path.isfile(celebrate_audio_path):
        print(f"No celebrate audio at {celebrate_audio_path}", flush=True)
        return None
    longest_clip = celebrate_audio_buffer_seconds - celebrate_audio_lead_seconds - celebrate_audio_tail_seconds
    try:
        result = subprocess.run(
            ['ffmpeg', '-nostdin', '-v', 'error', '-i', celebrate_audio_path,
             '-t', str(longest_clip), '-f', 's16le',
             '-ar', str(celebrate_audio_rate), '-ac', str(celebrate_audio_channels), 'pipe:1'],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout:
            print(f"Could not decode {celebrate_audio_path}: {result.stderr[-300:]!r}", flush=True)
            return None
        audio = silent_audio(celebrate_audio_lead_seconds) + result.stdout + silent_audio(celebrate_audio_tail_seconds)
        with open(celebrate_audio_buffer_path, 'r+b') as buffer:
            buffer.seek(celebrate_audio_buffer_data_offset)
            # The whole buffer, so nothing of a longer previous clip is left.
            buffer.write(audio.ljust(len(silent_audio(celebrate_audio_buffer_seconds)), b'\0'))
    except (subprocess.SubprocessError, OSError) as e:
        print(f"Could not load {celebrate_audio_path}: {e}", flush=True)
        return None
    return len(audio) / celebrate_audio_bytes_per_second

def program_audio_filters():
    # The celebrate audio is mixed in after the mute stage, so it plays even
    # while the game audio is muted. The buffer loops here, silenced, until
    # play_celebrate_audio() rewinds it and turns it up.
    audio_format = f'aformat=sample_rates={celebrate_audio_rate}:channel_layouts=stereo'
    return [
        f'[0:a]{audio_control_target}=volume={current_volume_level()},{audio_format}[a_game]',
        f'{celebrate_audio_source}=filename={celebrate_audio_buffer_path}:loop=0,{audio_format},{celebrate_audio_gain}=volume=0[a_celebrate]',
        '[a_game][a_celebrate]amix=inputs=2:duration=first:normalize=0,asplit=2[a_program][a_webrtc]',
    ]

def play_celebrate_audio(generation):
    if not is_ffmpeg_running():
        return
    with celebrate_audio_lock:
        duration = load_celebrate_audio()
        if duration is None:
            return
        send_ffmpeg_stdin_command(f'{celebrate_audio_source} -1 seek 0|0|1')
        send_ffmpeg_stdin_command(f'{celebrate_audio_gain} -1 volume 1.0')
    time.sleep(duration)
    if generation == celebrate_generation:
        send_ffmpeg_stdin_command(f'{celebrate_audio_gain} -1 volume 0')

def clear_celebrate_text(generation):
    global celebrate_text
    with state_lock:
        if generation != celebrate_generation:
            return
        celebrate_text = ''
    write_overlay_text()

def celebrate_goal(team_name):
    global celebrate_text, celebrate_generation
    with state_lock:
        celebrate_generation += 1
        generation = celebrate_generation
        celebrate_text = f'{team_name} Goal!'
    write_overlay_text()
    threading.Thread(target=play_celebrate_audio, args=(generation,), daemon=True).start()
    timer = threading.Timer(celebrate_text_seconds, clear_celebrate_text, args=(generation,))
    timer.daemon = True
    timer.start()

def send_ffmpeg_stdin_command(command):
    if not is_ffmpeg_running() or ffmpeg_process.stdin is None:
        print("FFmpeg stdin command skipped because FFmpeg is not ready", flush=True)
        return False

    try:
        # FFmpeg expects `c` as a single interactive keystroke, then the
        # command line separately after it prints the prompt.
        with ffmpeg_stdin_lock:
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
                penalty_game.advance(1)
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

def current_program_output_url():
    # The overlaid program feed: what the relay sends to YouTube, also served
    # as the HLS preview. preview_* and output_* are older names for the keys.
    output_base = config.get('program_output_url') or config.get('preview_output_url') or config.get('output_url') or 'rtmp://mediamtx/live/'
    output_key = config.get('program_stream_key') or config.get('preview_stream_key') or config.get('output_stream_key') or 'preview_hls'
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
    description = scoresheet_page_url()

    stream_response = service.liveStreams().insert(
        part='snippet,cdn,contentDetails,status',
        body={
            'snippet': {
                'title': title,
                'description': f'Created by easy-game-livestream for {selected_channel["title"]}',
            },
            'cdn': {
                'frameRate': youtube_stream_frame_rate,
                'ingestionType': 'rtmp',
                'resolution': youtube_stream_resolution,
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
                'description': description,
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
    socketio.emit('state_updated', current_state_payload())

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

    input_url = current_program_output_url()
    if not mediamtx_path_ready(stream_path_from_url(input_url)):
        return

    session_id = uuid.uuid4().hex
    try:
        relay_cmd = [
            'ffmpeg',
            '-rtmp_live', 'live',
            '-i', input_url,
            '-c', 'copy',
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
        socketio.emit('state_updated', current_state_payload())
    except Exception as e:
        print(f"Error starting relay FFmpeg: {e}", flush=True)
        relay_ffmpeg_process = None
        with relay_metrics_lock:
            relay_metrics_session_id = None

def overlay_video_filters(input_label, output_label, penalty_x, message_options, celebrate_y, layout):
    # The single definition of the overlay. Used for the live stream and for
    # the UI's "Show video" off preview (render_overlay_preview_jpeg), so the
    # two always look identical.
    box = f"box=1:boxcolor=black@0.69:boxborderw={layout['box_border']}"
    margin = layout['margin']
    # expansion=none so a typed "%" is shown as-is rather than parsed.
    message = ':'.join(
        [f"reload=1:expansion=none:fontcolor=white:fontsize={layout['message_font']}:line_spacing={layout['message_line_spacing']}:boxcolor=black@0.69:boxborderw={layout['box_border']}:bordercolor=black:x=(w-tw)/2:y=(h-th)/2"]
        + [f'{option}={value}' for option, value in message_options.items()]
    )
    return [
        f"[{input_label}]drawtext=fontfile={primary_font_path}:textfile={overlay_left_text_path}:reload=1:fontcolor=white:fontsize={layout['team_font']}:{box}:x={margin}:y={margin}[v_left]",
        f"[v_left]drawtext=fontfile={primary_font_path}:textfile={overlay_right_text_path}:reload=1:fontcolor=white:fontsize={layout['team_font']}:{box}:x=w-tw-{margin}:y={margin}[v_right]",
        f"[v_right]drawtext=fontfile={primary_font_path}:textfile={overlay_time_text_path}:reload=1:fontcolor=white:fontsize={layout['time_font']}:{box}:x=(w-tw)/2:y={margin}[v_time]",
        f"[v_time]drawtext=fontfile={primary_font_path}:textfile={overlay_phase_text_path}:reload=1:fontcolor=white:fontsize={layout['small_font']}:{box}:x={margin}:y=h-th-{margin}[v_phase]",
        f"[v_phase]{overlay_penalty_filters['left']}=fontfile={primary_font_path}:textfile={overlay_left_penalty_text_path}:reload=1:fontcolor=white:fontsize={layout['small_font']}:{box}:x={penalty_x['left']}:y={margin}[v_left_penalty]",
        f"[v_left_penalty]{overlay_penalty_filters['right']}=fontfile={primary_font_path}:textfile={overlay_right_penalty_text_path}:reload=1:fontcolor=white:fontsize={layout['small_font']}:{box}:x={penalty_x['right']}:y={margin}[v_right_penalty]",
        f"[v_right_penalty]drawtext=fontfile={primary_font_path}:textfile={overlay_mute_text_path}:reload=1:fontcolor=white:fontsize={layout['small_font']}:{box}:x=(w-tw)/2:y=h-th-{margin}[v_mute]",
        f"[v_mute]{overlay_celebrate_filter}=fontfile={primary_font_path}:textfile={overlay_celebrate_text_path}:reload=1:expansion=none:fontcolor=white:fontsize={layout['time_font']}:{box}:x=(w-tw)/2:y={celebrate_y}[v_celebrate]",
        f"[v_celebrate]{overlay_goal_filter}=fontfile={primary_font_path}:textfile={overlay_goal_text_path}:reload=1:expansion=none:fontcolor=white:fontsize={layout['goal_font']}:{box}:x=(w-tw)/2:y={celebrate_y}[v_goal]",
        f"[v_goal]{overlay_message_filters[0]}=fontfile={regular_font_path}:textfile={overlay_message_text_path}:{message}[v_message]",
        f"[v_message]{overlay_message_filters[1]}=fontfile={primary_font_path}:textfile={overlay_message_bold_text_path}:{message}[{output_label}]",
    ]

def render_overlay_preview_jpeg():
    write_overlay_text()
    resolution = incoming_video_resolution or default_video_resolution
    layout = overlay_layout(resolution)
    penalty_x = penalty_x_expressions(current_overlay_text(), layout)
    filter_complex = ';'.join(overlay_video_filters('0:v', 'v_out', penalty_x, message_filter_options(layout), celebrate_y_expression(layout), layout))
    result = subprocess.run(
        [
            'ffmpeg', '-v', 'error',
            '-f', 'lavfi', '-i', f'color=c={overlay_preview_background}:s={resolution}',
            '-filter_complex', filter_complex,
            '-map', '[v_out]', '-frames:v', '1',
            '-f', 'image2pipe', '-c:v', 'mjpeg', '-q:v', '4', '-',
        ],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace').strip() or 'no output')
    return result.stdout

def run_ffmpeg():
    global ffmpeg_process, ffmpeg_ready, sent_celebrate_y
    input_url = current_input_url()
    program_output_url = current_program_output_url()
    webrtc_output_url = current_webrtc_preview_output_url()
    
    try:
        stop_ffmpeg()
        ffmpeg_ready = False
        set_incoming_video_format(*probe_video_format(input_url))
        layout = overlay_layout(incoming_video_resolution)
        write_overlay_text(force=True)
        sent_penalty_x_expressions.clear()
        sent_penalty_x_expressions.update(penalty_x_expressions(current_overlay_text(), layout))
        sent_message_options.clear()
        sent_message_options.update(message_filter_options(layout))
        sent_celebrate_y = celebrate_y_expression(layout)
        create_celebrate_audio_buffer()
        filter_complex = ';'.join(overlay_video_filters('0:v', 'v_composited', sent_penalty_x_expressions, sent_message_options, sent_celebrate_y, layout) + [
            '[v_composited]split=2[v_program][v_webrtc]',
        ] + program_audio_filters())
        cmd = [
            'ffmpeg',
            # FFmpeg only reads stdin commands once per stats period (default
            # 0.5s), which is too slow for the rewind and turn-up of
            # play_celebrate_audio to land together.
            '-nostats',
            '-stats_period', '0.05',
            '-rtmp_live', 'live',
            '-i', input_url,
            '-filter_complex', filter_complex,
            '-map', '[v_program]',
            '-map', '[a_program]',
            '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p',
            '-preset', 'veryfast',
            '-tune', 'zerolatency',
            '-g', '30',
            '-keyint_min', '30',
            '-sc_threshold', '0',
            '-bf', '0',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-f', 'flv',
            program_output_url,
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
                set_incoming_video_format(None, None)
            elif running and not is_relay_ffmpeg_running():
                start_relay_ffmpeg()
        time.sleep(stream_monitor_interval_seconds)

def scoresheet_side_teams():
    return goal_teams.side_teams(scoresheet['goals'], state['left_team'], state['right_team'])

def overlay_team_name(side):
    # The name a goal entered on this side is recorded under, as in
    # record_goal.
    return state[f'{side}_team'] or side.capitalize()

def goal_team(goal, side_teams):
    # The scoring team's name: a manual goal's own, or the scoresheet side's
    # team, which is None until the rosters show which team it is.
    return goal.team_name if is_manual_goal(goal) else side_teams.get(goal.team)

def goal_celebrates(goal, side_teams):
    # A goal is celebrated only once its side's team is known from the
    # rosters, so an opponent's goal isn't celebrated by mistake while both
    # overlay teams still have the same name.
    # When both overlay sides have that name, either side's Celebrate will do.
    return scoring_side(goal, side_teams) is not None

def scoring_side(goal, side_teams):
    # The overlay side ('left' or 'right') whose team panel lists the goal in
    # its Scoring section: the side with the scoring team's name and Celebrate
    # on. None until the rosters show which team scored it. When both sides
    # have that name, the left one.
    team_name = goal_team(goal, side_teams)
    if not team_name:
        return None
    for side in ('left', 'right'):
        if overlay_team_name(side) == team_name and state[f'{side}_celebrate']:
            return side
    return None

manual_strength_values = {'even': 'even', 'power_play': 'pp', 'shorthanded': 'shg'}

def manual_goal_entry(goal):
    # The goal as the entry form's fields, for editing it.
    return {
        'period': goal.period,
        'clock': goal.clock,
        'scorer': player_text(goal.scorer),
        'assists': [player_text(assist) for assist in goal.assists],
        'strength': manual_strength_values[goal.strength.value],
        'empty_net': goal.empty_net,
    }

def phase_period(phase_name):
    # The period number for a goal entered during a phase: the number in its
    # name ("Period 2"), else the period it follows (a Flood after Period 2
    # is 2), or the next one for a timed phase such as OT.
    match = re.search(r'\d+', phase_name or '')
    if match:
        return int(match.group(0))
    period = 0
    for phase in phase_configurations:
        if phase['name'] == phase_name:
            break
        match = re.search(r'\d+', phase['name'])
        if match:
            period = int(match.group(0))
    return max(1, period + 1 if phase_has_duration(phase_name) else period)

def goal_entry_defaults(side):
    team = Team.HOME if side == 'left' else Team.AWAY
    opponent = Team.AWAY if team is Team.HOME else Team.HOME
    skaters = penalty_game.skaters_for(team)
    opponent_skaters = penalty_game.skaters_for(opponent)
    timed = phase_has_duration(state['phase'])
    return {
        'period': phase_period(state['phase']),
        'clock': format_overlay_clock(state['time']) if timed else '00:00',
        'strength': 'pp' if skaters > opponent_skaters else 'shg' if skaters < opponent_skaters else 'even',
        # The other side pulled its goalie.
        'empty_net': bool(state['right_en' if side == 'left' else 'left_en']),
    }

def goal_order(goal):
    return goal.period, -(parse_clock(goal.clock) or 0)

def current_scoresheet_payload():
    side_teams = scoresheet_side_teams()
    goals = []
    for goal in sorted(scoresheet['goals'] + manual_goals.goals, key=goal_order):
        manual = is_manual_goal(goal)
        entry = dict(
            goal_teams.describe(goal, goal_team(goal, side_teams)),
            id=goal.id,
            period=goal.period,
            clock=goal.clock,
            settled=goal_is_settled(goal.id),
            side=scoring_side(goal, side_teams),
            manual=manual,
        )
        if manual:
            entry['entry'] = manual_goal_entry(goal)
        goals.append(entry)
    rosters = {}
    entry_defaults = {}
    for side in ('left', 'right'):
        roster = goal_teams.roster(overlay_team_name(side))
        rosters[side] = [
            {'jersey': jersey, 'name': name}
            for jersey, name in sorted(roster.items(), key=lambda item: (not item[0].isdigit(), int(item[0]) if item[0].isdigit() else 0, item[0]))
        ]
        entry_defaults[side] = manual_goal_defaults[side] or goal_entry_defaults(side)
    return {
        'enabled': bool(scoresheet_source()),
        'goals': goals,
        'rosters': rosters,
        'entry_defaults': entry_defaults,
        'game_over': scoresheet['game_over'],
        'error': scoresheet['error'],
        'not_started': scoresheet['not_started'],
        'checked_at': scoresheet['checked_at'],
        'showing': scoresheet['showing'],
    }

def show_goal_caption(goal_id):
    scoresheet['showing'] = goal_id
    scoresheet['showing_until'] = time.monotonic() + goal_overlay_seconds
    if goal_id in scoresheet['queue']:
        scoresheet['queue'].remove(goal_id)

def advance_goal_caption():
    # Clears the caption once its time is up and shows the next queued goal.
    # A goal still being entered stays up until it settles and its time is
    # up. Returns whether the caption changed.
    showing = scoresheet['showing']
    if (showing is not None and goal_is_settled(showing)
            and time.monotonic() >= scoresheet['showing_until']):
        scoresheet['showing'] = None
    if scoresheet['showing'] is None and scoresheet['queue']:
        show_goal_caption(scoresheet['queue'][0])
    return scoresheet['showing'] != showing

def poll_scoresheet(feed):
    # Returns whether the overlay or anything in current_state_payload changed.
    error = ''
    not_started = False
    try:
        changes = feed.poll()
    except GameNotStarted:
        changes = None
        not_started = True
    except ScoresheetError as e:
        changes = None
        error = str(e)
    with state_lock:
        changed = error != scoresheet['error'] or not_started != scoresheet['not_started']
        scoresheet['error'] = error
        scoresheet['not_started'] = not_started
        # Epoch seconds; the UI shows it in the browser's time zone.
        scoresheet['checked_at'] = time.time()
        if changes is not None:
            scoresheet['goals'] = feed.goals
            scoresheet['settled'] = {goal.id for goal in feed.goals if feed.is_settled(goal.id)}
            changed = changed or bool(changes) or feed.game_over != scoresheet['game_over']
            scoresheet['game_over'] = feed.game_over
            side_teams = scoresheet_side_teams()
            for change in changes:
                goal = change.goal
                # Goal IDs and times only: the names stay out of the logs.
                print(f"Scoresheet goal {change.kind.value}: {goal.id} P{goal.period} {goal.clock} {goal.team}", flush=True)
                if change.kind is GoalChangeKind.REMOVED:
                    if goal.id in scoresheet['queue']:
                        scoresheet['queue'].remove(goal.id)
                    if scoresheet['showing'] == goal.id:
                        scoresheet['showing'] = None
                    continue
                if change.kind in (GoalChangeKind.ADDED, GoalChangeKind.UPDATED):
                    replace_manual_goal(goal, side_teams)
                # Goals on the scoresheet when the app starts are already
                # settled; only new ones are shown and sped up for.
                new_goal = not feed.is_settled(goal.id)
                if change.kind is GoalChangeKind.ADDED and new_goal:
                    scoresheet['fast_poll_until'] = time.monotonic() + scoresheet_new_goal_fast_seconds
                if change.kind is GoalChangeKind.SETTLED and scoresheet['showing'] == goal.id:
                    # Keep the finished caption up for the full time.
                    scoresheet['showing_until'] = max(
                        scoresheet['showing_until'], time.monotonic() + goal_overlay_seconds)
                # Shown as soon as it's reported, and filled in as the
                # scorekeeper adds the assists. Checked on updates too, in
                # case the team was only known once the goal was filled in.
                if (change.kind in (GoalChangeKind.ADDED, GoalChangeKind.UPDATED) and new_goal
                        and state['goal_auto_show'] and goal.id not in scoresheet['auto_shown']
                        and goal_celebrates(goal, side_teams)):
                    scoresheet['auto_shown'].add(goal.id)
                    scoresheet['queue'].append(goal.id)
        # A caption that comes or goes here changes the overlay too.
        changed = advance_goal_caption() or changed
    if error and changed:
        print(f"Scoresheet poll failed: {error}", flush=True)
    return changed

def replace_manual_goal(goal, side_teams):
    # The scoresheet has caught up with a goal entered by hand: list the
    # scoresheet's goal instead, and don't show its caption again.
    team_name = side_teams.get(goal.team)
    replaced = manual_goals.take_match(team_name, goal) if team_name else None
    if replaced is None:
        return
    print(f"Scoresheet goal {goal.id} replaces manual goal {replaced.id}", flush=True)
    scoresheet['auto_shown'].add(goal.id)
    scoresheet['queue'] = [goal.id if queued == replaced.id else queued for queued in scoresheet['queue']]
    if scoresheet['showing'] == replaced.id:
        scoresheet['showing'] = goal.id

def monitor_scoresheet():
    try:
        feed = open_feed(scoresheet_source())
    except (ScoresheetError, ValueError) as e:
        print(f"Scoresheet goals are off: {e}", flush=True)
        with state_lock:
            scoresheet['error'] = str(e)
        return
    last_poll = None
    while True:
        changed = False
        # Worked out on every pass, so a goal ("+" button or a new
        # scoresheet goal) speeds up a slow wait that's already under way.
        with state_lock:
            fast = time.monotonic() < scoresheet['fast_poll_until']
        if fast:
            interval = scoresheet_new_goal_poll_seconds
        elif feed.game_over and feed.all_settled:
            # Keep checking slowly after the game, for late corrections.
            interval = scoresheet_finished_poll_seconds
        else:
            interval = scoresheet_poll_seconds
        if last_poll is None or time.monotonic() >= last_poll + interval:
            last_poll = time.monotonic()
            changed = poll_scoresheet(feed)
            with state_lock:
                scoresheet_status = current_scoresheet_payload()
            # Just the goal list, so the UI's "checked at" time stays current
            # without a full state update on every poll.
            socketio.emit('scoresheet_updated', scoresheet_status)
        with state_lock:
            changed = advance_goal_caption() or changed
            updated_state = current_state_payload() if changed else None
        if updated_state is not None:
            write_overlay_text()
            socketio.emit('state_updated', updated_state)
        time.sleep(0.5)

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
        if 'message_text' in data:
            data['message_text'] = str(data['message_text']).replace('\r\n', '\n').replace('\r', '\n').strip()
        if 'celebrate_position' in data and data['celebrate_position'] not in celebrate_positions:
            data.pop('celebrate_position')
        if 'clock_mode' in data and data['clock_mode'] not in ('stop_time', 'run_time'):
            data['clock_mode'] = 'stop_time'
        state.update(data)
        if phase_changed:
            apply_phase_defaults(state['phase'])
        if clock_adjustment_seconds:
            adjust_pp_times(clock_adjustment_seconds)
            penalty_game.adjust_remaining_seconds(clock_adjustment_seconds)
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

def scoresheet_source():
    # scoresheet_url is a game's scoresheet URL, or a mock game file (see
    # scoresheets/mock.py). A mock file without a directory is in the config
    # directory.
    url = str(config.get('scoresheet_url') or '')
    if is_mock_game(url) and not os.path.isabs(url):
        return os.path.join(os.path.dirname(load_config_path), url)
    return url

def scoresheet_page_url():
    # The game's web page, or '' for a mock game.
    url = str(config.get('scoresheet_url') or '')
    return '' if is_mock_game(url) else url

def scoresheet_embed_url():
    # The game's score sheet page, shown in the UI in an iframe. For a
    # gamesheetstats.com page, hide its navigation and links unless the URL
    # already sets them.
    url = scoresheet_page_url()
    if not url:
        return ''
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname or not parts.hostname.endswith('gamesheetstats.com'):
        return url
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    existing_keys = {key for key, _ in query}
    for key in ('configuration[navigation]', 'configuration[links]'):
        if key not in existing_keys:
            query.append((key, 'false'))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))

@app.route('/')
def index():
    toggle_buffer_ms = config.get('toggle_buffer_ms', 400)
    start_show_video = config.get('start_show_video', True)
    return render_template(
        'index.html',
        webrtc_additional_hosts=os.getenv('MTX_WEBRTCADDITIONALHOSTS', ''),
        toggle_buffer_ms=toggle_buffer_ms,
        start_show_video=start_show_video,
        scoresheet_url=scoresheet_embed_url(),
        scoresheet_enabled=bool(scoresheet_source()),
        phases=phase_configurations,
        initial_phase=initial_phase_name,
    )

@app.route('/api/overlay-preview.jpg')
def overlay_preview():
    try:
        jpeg = render_overlay_preview_jpeg()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Overlay preview render failed: {error}", flush=True)
        return jsonify({'error': 'overlay preview render failed'}), 500
    return Response(jpeg, mimetype='image/jpeg', headers={'Cache-Control': 'no-store'})

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
    for key in ('left_pp', 'left_en', 'right_pp', 'right_en', 'mute_on_stop', 'message_show', 'message_bold', 'message_centered', 'message_background', 'goal_auto_show', 'left_celebrate', 'right_celebrate'):
        if key in data and not isinstance(data[key], bool):
            data[key] = str(data[key]).lower() in ('1', 'true', 'yes', 'on')
    if 'clock_running' in data and not isinstance(data['clock_running'], bool):
        data['clock_running'] = str(data['clock_running']).lower() in ('1', 'true', 'yes', 'on')
    updated_state = apply_overlay_update(data)
    return jsonify(updated_state)

@app.route('/api/penalties', methods=['POST'])
def add_penalty():
    data = request.get_json(silent=True) or {}
    player = str(data.get('player', 'Player')).strip()
    try:
        team = Team(data.get('team'))
        kind = PenaltyType(data.get('kind', PenaltyType.MINOR.value))
        if not player:
            raise ValueError('player is required')
        with state_lock:
            penalty_game.assess_penalty(team, player, kind)
            updated_state = current_state_payload()
    except (TypeError, ValueError) as error:
        return jsonify({'error': str(error)}), 400
    write_overlay_text()
    socketio.emit('state_updated', updated_state)
    return jsonify(updated_state)

@app.route('/api/penalties/<int:penalty_id>', methods=['DELETE', 'PATCH'])
def update_penalty(penalty_id):
    with state_lock:
        if request.method == 'DELETE':
            penalty = penalty_game.cancel_penalty(penalty_id)
        else:
            data = request.get_json(silent=True) or {}
            try:
                if 'kind' in data:
                    penalty = penalty_game.set_penalty_kind(penalty_id, PenaltyType(data['kind']))
                elif 'remaining_time' in data:
                    seconds = parse_clock(data['remaining_time'])
                    if seconds is None:
                        return jsonify({'error': 'remaining_time must be a valid M:SS value'}), 400
                    penalty = penalty_game.set_remaining_seconds(penalty_id, seconds)
                else:
                    return jsonify({'error': 'kind or remaining_time is required'}), 400
            except (TypeError, ValueError) as error:
                return jsonify({'error': str(error)}), 400
        if penalty is None:
            return jsonify({'error': 'active penalty not found'}), 404
        updated_state = current_state_payload()
    write_overlay_text()
    socketio.emit('state_updated', updated_state)
    return jsonify(updated_state)

@app.route('/api/goals/<team>', methods=['POST'])
def record_goal(team):
    try:
        scoring_team = Team(team)
    except ValueError:
        return jsonify({'error': 'team must be home or away'}), 400
    with state_lock:
        score_key = 'left_score' if scoring_team is Team.HOME else 'right_score'
        state[score_key] = str(int(normalize_score_value(state[score_key])) + 1)
        side = 'left' if scoring_team is Team.HOME else 'right'
        manual_goal_defaults[side] = goal_entry_defaults(side)
        penalty_game.record_goal(scoring_team)
        celebrating_team = (state[f'{side}_team'] or side.capitalize()) if state[f'{side}_celebrate'] else None
        # The goal should reach the scoresheet soon, so check it often.
        scoresheet['fast_poll_until'] = time.monotonic() + scoresheet_new_goal_fast_seconds
        updated_state = current_state_payload()
    if celebrating_team:
        celebrate_goal(celebrating_team)
    write_overlay_text()
    socketio.emit('state_updated', updated_state)
    return jsonify(updated_state)

@app.route('/api/scoresheet/goals/<goal_id>/show', methods=['POST'])
def show_scoresheet_goal(goal_id):
    with state_lock:
        if scoresheet_goal(goal_id) is None:
            return jsonify({'error': 'no such goal'}), 404
        show_goal_caption(goal_id)
        updated_state = current_state_payload()
    write_overlay_text()
    socketio.emit('state_updated', updated_state)
    return jsonify(updated_state)

@app.route('/api/scoresheet/manual-goals', methods=['POST'])
def add_manual_goal():
    data = request.get_json(silent=True) or {}
    side = data.get('side')
    if side not in ('left', 'right'):
        return jsonify({'error': 'side must be left or right'}), 400
    with state_lock:
        try:
            goal = manual_goals.add(overlay_team_name(side), data)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        # The next goal's defaults come from its own "+" press, or the clock.
        manual_goal_defaults[side] = None
        print(f"Manual goal added: {goal.id} P{goal.period} {goal.clock} {side}", flush=True)
        if state['goal_auto_show'] and goal_celebrates(goal, scoresheet_side_teams()):
            scoresheet['auto_shown'].add(goal.id)
            scoresheet['queue'].append(goal.id)
            advance_goal_caption()
        updated_state = current_state_payload()
    write_overlay_text()
    socketio.emit('state_updated', updated_state)
    return jsonify(updated_state)

@app.route('/api/scoresheet/manual-goals/<goal_id>', methods=['PUT'])
def update_manual_goal(goal_id):
    data = request.get_json(silent=True) or {}
    with state_lock:
        if manual_goals.get(goal_id) is None:
            return jsonify({'error': 'no such goal'}), 404
        try:
            manual_goals.update(goal_id, data)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        print(f"Manual goal updated: {goal_id}", flush=True)
        updated_state = current_state_payload()
    write_overlay_text()
    socketio.emit('state_updated', updated_state)
    return jsonify(updated_state)

@app.route('/api/scoresheet/manual-goals/<goal_id>', methods=['DELETE'])
def delete_manual_goal(goal_id):
    with state_lock:
        if manual_goals.remove(goal_id) is None:
            return jsonify({'error': 'no such goal'}), 404
        print(f"Manual goal deleted: {goal_id}", flush=True)
        if goal_id in scoresheet['queue']:
            scoresheet['queue'].remove(goal_id)
        if scoresheet['showing'] == goal_id:
            scoresheet['showing'] = None
            advance_goal_caption()
        updated_state = current_state_payload()
    write_overlay_text()
    socketio.emit('state_updated', updated_state)
    return jsonify(updated_state)

@app.route('/api/scoresheet/hide', methods=['POST'])
def hide_scoresheet_goal():
    # Hides the caption and drops any goals waiting to be shown.
    with state_lock:
        scoresheet['showing'] = None
        scoresheet['queue'].clear()
        updated_state = current_state_payload()
    write_overlay_text()
    socketio.emit('state_updated', updated_state)
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
            'error': f'Missing Google OAuth client configuration. Add google_oauth_client_secret.json to config/ or set google_oauth_client_id/google_oauth_client_secret in {config_filename}.'
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
    if scoresheet_source():
        threading.Thread(target=monitor_scoresheet, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
