import os
import subprocess
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import parse_qs
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import ffmpeg

# Load config
config_path = '/app/config/config.json'
if os.path.exists(config_path):
    with open(config_path) as f:
        config = json.load(f)
else:
    config = {}

state = {
    'home_team': 'Home',
    'home_score': '0',
    'home_pp': False,
    'home_en': False,
    'away_team': 'Away',
    'away_score': '0',
    'away_pp': False,
    'away_en': False,
    'period': 'Period 1',
    'time': '20:00',
    'mute': True,
    'mute_on_stop': True,
    'running': False,
}
ffmpeg_process = None
relay_ffmpeg_process = None
process_lock = threading.Lock()
publish_start_generation = 0
ffmpeg_ready = False
overlay_paths = {
    'home': '/tmp/overlay-home.txt',
    'away': '/tmp/overlay-away.txt',
    'period': '/tmp/overlay-period.txt',
    'time': '/tmp/overlay-time.txt',
}
muted_volume_level = '0.001'
normal_volume_level = '1.0'
stream_monitor_interval_seconds = float(config.get('stream_monitor_interval_seconds', 1.0))
mediamtx_api_url = config.get('mediamtx_api_url', 'http://mediamtx:9997/v3/paths/list')
last_stream_ready_state = None
audio_control_target = 'volume@audio_gain'
state_lock = threading.Lock()

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
    }

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

def write_overlay_text():
    text_by_section = current_overlay_text()
    for section, text in text_by_section.items():
        output_path = overlay_paths[section]
        temp_path = f"{output_path}.tmp"
        with open(temp_path, 'w', encoding='utf-8') as overlay_file:
            overlay_file.write(text)
        os.replace(temp_path, output_path)
    print(f"Overlay text file updated: {text_by_section}", flush=True)

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
            if not state['running']:
                continue
            remaining_seconds = parse_clock(state['time'])
            if remaining_seconds is None:
                continue
            if remaining_seconds <= 0:
                state['running'] = False
                if state['mute_on_stop']:
                    state['mute'] = True
                updated_state = dict(state)
            else:
                state['time'] = format_clock(remaining_seconds - 1)
                if parse_clock(state['time']) == 0:
                    state['running'] = False
                    if state['mute_on_stop']:
                        state['mute'] = True
                updated_state = dict(state)
        write_overlay_text()
        socketio.emit('state_updated', updated_state)

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
    output_key = config.get('preview_stream_key') or config.get('output_stream_key') or 'preview'
    return build_rtmp_url(output_base, output_key)

def current_upstream_output_url():
    output_base = config.get('youtube_output_url') or ''
    output_key = os.getenv('YOUTUBE_STREAM_KEY') or config.get('youtube_stream_key') or ''
    if not output_base:
        return ''
    return build_rtmp_url(output_base, output_key)

def current_stream_path():
    parsed = urllib.parse.urlparse(current_input_url())
    path = parsed.path.strip('/')
    return path

def is_stream_ready():
    global last_stream_ready_state
    stream_path = current_stream_path()
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

def start_relay_ffmpeg():
    global relay_ffmpeg_process
    upstream_url = current_upstream_output_url()
    if not upstream_url:
        return
    if is_relay_ffmpeg_running():
        return

    input_url = current_preview_output_url()
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
    output_url = current_preview_output_url()
    
    try:
        stop_ffmpeg()
        ffmpeg_ready = False
        write_overlay_text()
        stream = ffmpeg.input(input_url, rtmp_live='live')
        video = (
            stream.video
            .filter(
                'drawtext',
                textfile=overlay_paths['home'],
                reload=1,
                fontsize=50,
                fontcolor='white',
                x=10,
                y=10,
                box=0,
            )
            .filter(
                'drawtext',
                textfile=overlay_paths['away'],
                reload=1,
                fontsize=50,
                fontcolor='white',
                x='w-tw-10',
                y=10,
                box=0,
            )
            .filter(
                'drawtext',
                textfile=overlay_paths['time'],
                reload=1,
                fontsize=56,
                fontcolor='white',
                x='(w-tw)/2',
                y=10,
                box=0,
            )
            .filter(
                'drawtext',
                textfile=overlay_paths['period'],
                reload=1,
                fontsize=42,
                fontcolor='white',
                x=10,
                y='h-th-(h*0.05)',
                box=0,
            )
        )
        audio = stream.audio.filter(audio_control_target, volume=current_volume_level())
        
        out = ffmpeg.output(
            video,
            audio,
            output_url,
            f='flv',
            vcodec='libx264',
            acodec='aac',
            preset='veryfast',
            tune='zerolatency',
            pix_fmt='yuv420p',
            g=30,
            keyint_min=30,
            sc_threshold=0,
            bf=0,
        )
        cmd = ['ffmpeg'] + ffmpeg.get_args(out)
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
        time.sleep(stream_monitor_interval_seconds)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

def apply_overlay_update(data):
    global state
    with state_lock:
        previous_mute = state['mute']
        if 'home_score' in data:
            data['home_score'] = normalize_score_value(data['home_score'])
        if 'away_score' in data:
            data['away_score'] = normalize_score_value(data['away_score'])
        state.update(data)
        if 'mute_on_stop' in data and state['mute_on_stop'] and not state['running']:
            state['mute'] = True
        if 'running' in data:
            state['running'] = bool(data['running'])
            if state['mute_on_stop']:
                state['mute'] = not state['running']
        updated_state = dict(state)
    print("Updating overlay with data:", data, flush=True)
    if updated_state['mute'] != previous_mute:
        with process_lock:
            if is_ffmpeg_running() and ffmpeg_ready:
                if not update_live_volume():
                    print("Live volume update failed; keeping FFmpeg running", flush=True)
            else:
                print("FFmpeg not ready; mute state will apply on next FFmpeg start", flush=True)
    write_overlay_text()
    socketio.emit('state_updated', updated_state)
    return updated_state

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/state', methods=['GET'])
def get_state():
    return jsonify(state)

@app.route('/api/state', methods=['POST'])
def update_state():
    data = request.get_json(silent=True) or request.form.to_dict()
    if 'mute' in data and not isinstance(data['mute'], bool):
        data['mute'] = str(data['mute']).lower() in ('1', 'true', 'yes', 'on')
    for key in ('home_pp', 'home_en', 'away_pp', 'away_en', 'mute_on_stop'):
        if key in data and not isinstance(data[key], bool):
            data[key] = str(data[key]).lower() in ('1', 'true', 'yes', 'on')
    if 'running' in data and not isinstance(data['running'], bool):
        data['running'] = str(data['running']).lower() in ('1', 'true', 'yes', 'on')
    updated_state = apply_overlay_update(data)
    return jsonify(updated_state)

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
    threading.Thread(target=monitor_stream, daemon=True).start()
    threading.Thread(target=tick_game_clock, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
