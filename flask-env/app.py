import os
import subprocess
import json
import re
import time
import shutil
import tempfile
import threading
import queue
import uuid
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from werkzeug.utils import secure_filename
from gtts import gTTS
from pydub import AudioSegment

# Load secrets/config from a .env file (repo root or CWD). See .env.example.
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, '.env'))

app = Flask(__name__)

# --- Configuration (env-overridable; see .env.example) ---
SOUND_FOLDER = os.environ.get('SOUND_FOLDER', '/home/rs/flask-env/wav')
TTS_CACHE_FOLDER = os.environ.get('TTS_CACHE_FOLDER', '/home/rs/flask-env/tts_cache')
DEVICE_LABELS_FILE = 'devices.json'
CATEGORIES_FILE = 'categories.json'
FFMPEG_PATH = '/usr/bin/ffmpeg'
AMIXER_PATH = '/usr/bin/amixer'
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(32).hex()
DEFAULT_API_KEY = os.environ.get('DEFAULT_API_KEY', '')
SERVER_PORT = int(os.environ.get('SOUND_SERVER_PORT', 5000))
REPEAT_DELAY_SECONDS = 0.2
MAX_REPEAT_COUNT = 20

# --- Playback Queue and Worker Thread ---
playback_queue = queue.Queue()

def audio_worker():
    print("Audio worker thread started.")
    while True:
        try:
            job = playback_queue.get()
            print(f"WORKER: Got job: {job}")
            device_id = job.get('device_id')
            fg_path = job.get('filepath')
            bg_path = job.get('background_path')
            is_fg_temp = job.get('is_temp', False)
            
            final_play_path = fg_path
            final_is_temp = is_fg_temp

            if bg_path and os.path.exists(bg_path) and os.path.exists(fg_path):
                try:
                    print(f"WORKER: Mixing '{os.path.basename(fg_path)}' over '{os.path.basename(bg_path)}'")
                    foreground_audio = AudioSegment.from_wav(fg_path)
                    background_audio = AudioSegment.from_wav(bg_path)
                    mixed_audio = background_audio.overlay(foreground_audio)
                    mixed_filename = f"mixed_{uuid.uuid4().hex}.wav"
                    mixed_filepath = os.path.join(TTS_CACHE_FOLDER, mixed_filename)
                    mixed_audio.export(mixed_filepath, format="wav")
                    final_play_path = mixed_filepath
                    final_is_temp = True
                except Exception as e:
                    print(f"WORKER ERROR: Failed to mix audio, playing foreground only. Error: {e}")
            
            if device_id and final_play_path:
                do_play_sound(device_id, final_play_path)

            if final_is_temp and os.path.exists(final_play_path):
                os.remove(final_play_path)
                print(f"WORKER: Deleted final temp file: {final_play_path}")

            if is_fg_temp and final_play_path != fg_path and os.path.exists(fg_path):
                os.remove(fg_path)
                print(f"WORKER: Deleted original temp TTS file: {fg_path}")

            playback_queue.task_done()
        except Exception as e:
            print(f"WORKER ERROR: An exception occurred: {e}"); import traceback; traceback.print_exc()

def do_play_sound(device_id_str, filepath_str):
    if not os.path.isfile(filepath_str):
        msg = f"File not found: {filepath_str}"; print(f"PLAY_SOUND_ERROR: {msg}"); return False, msg
    if ".." in device_id_str:
        msg = f"Invalid path components in '{device_id_str}'"; print(f"PLAY_SOUND_ERROR: {msg}"); return False, msg
    device_hw = f"hw:{device_id_str}"; cmd = ["/usr/bin/aplay", "-D", device_hw, filepath_str]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        msg = f"Successfully played {os.path.basename(filepath_str)} on {device_hw}"; print(f"PLAY_SOUND_SUCCESS: {msg}"); return True, msg
    except subprocess.CalledProcessError as e:
        err_msg = f"aplay error: {e.stderr or e.stdout or 'Unknown'}"; print(f"PLAY_SOUND_ERROR: playing {os.path.basename(filepath_str)} on {device_hw}: {err_msg}"); return False, err_msg
    except FileNotFoundError: msg = f"'{cmd[0]}' not found."; print(f"PLAY_SOUND_ERROR: {msg}"); return False, msg
    except Exception as e: msg = f"Unexpected error during play: {e}"; print(f"PLAY_SOUND_ERROR: {msg}"); return False, msg

def load_labels():
    if os.path.exists(DEVICE_LABELS_FILE):
        try:
            with open(DEVICE_LABELS_FILE, 'r') as f: content = f.read(); return json.loads(content) if content.strip() else {}
        except Exception as e: print(f"Warning: Could not load/parse {DEVICE_LABELS_FILE}: {e}."); return {}
    return {}
def save_labels(labels):
    try:
        with open(DEVICE_LABELS_FILE, 'w') as f: json.dump(labels, f, indent=2)
    except Exception as e: print(f"Error saving labels to {DEVICE_LABELS_FILE}: {e}")
def load_categories():
    if os.path.exists(CATEGORIES_FILE):
        try:
            with open(CATEGORIES_FILE, 'r') as f: content = f.read(); return json.loads(content) if content.strip() else {}
        except Exception as e: print(f"Warning: Could not load/parse {CATEGORIES_FILE}: {e}."); return {}
    return {}
def save_categories(categories):
    try:
        with open(CATEGORIES_FILE, 'w') as f: json.dump(categories, f, indent=2); return True
    except Exception as e: print(f"Error saving categories to {CATEGORIES_FILE}: {e}"); return False

def discover_and_label_audio_devices():
    all_card_labels = load_labels(); discovered_devices_list = []; unique_cards_dict = {}
    try:
        aplay_cmd = ['/usr/bin/aplay', '-l']; aplay_out = subprocess.check_output(aplay_cmd, text=True, stderr=subprocess.STDOUT)
        line_regex = re.compile(r"^card\s+(\d+):\s*([^\[]+)\s*\[.*?\]\s*,\s*device\s+(\d+):\s*([^\[]+)\s*\[.*?\]")
        for line in aplay_out.splitlines():
            match = line_regex.match(line.strip())
            if match:
                card_idx, card_name, dev_idx, dev_name = match.groups(); card_name = card_name.strip(); dev_name = dev_name.strip()
                if card_idx not in unique_cards_dict: unique_cards_dict[card_idx] = { 'card_index': card_idx, 'default_name': card_name, 'user_label': all_card_labels.get(card_idx, '') }
                hw_id = f"{card_idx},{dev_idx}"; usr_lbl = all_card_labels.get(card_idx)
                ha_name = f"{usr_lbl} (Dev {dev_idx})" if usr_lbl else f"{card_name}, Dev {dev_idx}"
                disp_lbl = f"{ha_name} (hw:{hw_id})"
                discovered_devices_list.append({'hw_id': hw_id, 'card_index': card_idx, 'ha_name': ha_name, 'display_label_for_table': disp_lbl})
        return discovered_devices_list, unique_cards_dict, None
    except Exception as e: return [], {}, f"Error listing devices: {e}"

@app.route('/')
def index():
    files = []; discovered_devices_info, unique_cards_dict, discovery_error = discover_and_label_audio_devices()
    if os.path.exists(SOUND_FOLDER) and os.path.isdir(SOUND_FOLDER): files = sorted([f for f in os.listdir(SOUND_FOLDER) if f.lower().endswith('.wav')])
    else: flash(f"Sound folder '{SOUND_FOLDER}' not found.", "warning")
    if discovery_error: flash(discovery_error, "error")
    return render_template('index.html', files=files, discovered_devices=discovered_devices_info,
                           cards_for_labeling=list(unique_cards_dict.values()),
                           default_api_key=DEFAULT_API_KEY, SOUND_FOLDER=SOUND_FOLDER)

@app.route('/play/<device_id>/<filename>', defaults={'count': 1})
@app.route('/play/<device_id>/<filename>/<int:count>')
def play_route(device_id, filename, count):
    if not (1 <= count <= MAX_REPEAT_COUNT): return jsonify(success=False, message=f"Repeat count out of range."), 400
    filepath = os.path.join(SOUND_FOLDER, filename)
    if ".." in filename or filename.startswith(("/", "\\")) or not os.path.exists(filepath):
        return jsonify(success=False, message="Invalid or non-existent filename."), 400
    background_sound = request.args.get('background', None)
    background_path = None
    if background_sound:
        bg_candidate_path = os.path.join(SOUND_FOLDER, background_sound)
        if ".." not in background_sound and os.path.exists(bg_candidate_path): background_path = bg_candidate_path
        else: print(f"Warning: Requested background sound '{background_sound}' not found.")
    for _ in range(count):
        job = {"device_id": device_id, "filepath": filepath, "background_path": background_path, "is_temp": False}
        playback_queue.put(job); print(f"API: Queued job: {job}")
    return jsonify(success=True, message=f"Queued {count} play(s) of {filename}."), 202

@app.route('/update_label', methods=['POST'])
def update_label():
    labels, updated = load_labels(), False
    for k, v in request.form.items():
        if k.startswith('label_for_card_'):
            idx, lbl = k.replace('label_for_card_', ''), v.strip(); current_label = labels.get(idx)
            if current_label != lbl:
                if lbl: labels[idx] = lbl
                elif idx in labels: del labels[idx]
                updated = True
    if updated: save_labels(labels)
    return jsonify(success=True, message="Labels processed.")

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files: flash('No file part.', 'warning'); return redirect(url_for('index'))
    file = request.files['file']
    if file.filename == '': flash('No file selected.', 'warning'); return redirect(url_for('index'))
    os.makedirs(SOUND_FOLDER, exist_ok=True)
    orig_fn = file.filename; sec_fn = secure_filename(orig_fn); base, ext = os.path.splitext(sec_fn); ext = ext.lower()
    tgt_fn, final_path = base + ".wav", os.path.join(SOUND_FOLDER, base + ".wav"); conv_exts = ['.mp3']
    try:
        if ext == '.wav': file.save(final_path); flash(f"WAV '{tgt_fn}' uploaded!", "success")
        elif ext in conv_exts:
            with tempfile.TemporaryDirectory(prefix="audio_conv_") as temp_dir:
                tmp_up = os.path.join(temp_dir, sec_fn); file.save(tmp_up)
                tmp_cvt = os.path.join(temp_dir, tgt_fn); cmd_ff = [FFMPEG_PATH, '-y', '-i', tmp_up, tmp_cvt]
                proc = subprocess.run(cmd_ff, capture_output=True, text=True, check=False)
                if proc.returncode == 0 and os.path.exists(tmp_cvt):
                    shutil.move(tmp_cvt, final_path); flash(f"'{orig_fn}' converted to '{tgt_fn}'!", "success")
                else: flash(f"Convert Fail '{orig_fn}'. FFMPEG: {(proc.stderr or proc.stdout or 'Error')[:500]}", "error")
        else: flash(f"Unsupported: '{ext}'. Use .wav or {', '.join(conv_exts)}.", "warning")
    except Exception as e: flash(f"Err proc '{orig_fn}': {str(e)[:500]}", "error")
    return redirect(url_for('index'))

@app.route('/delete/<path:filename>', methods=['POST'])
def delete_file(filename):
    if ".." in filename or filename.startswith(("/", "\\")): return jsonify(success=False, message="Invalid path"), 400
    fp = os.path.normpath(os.path.join(SOUND_FOLDER, filename)); sfn = os.path.normpath(SOUND_FOLDER)
    if not (fp.startswith(sfn + os.sep) or fp == sfn): return jsonify(success=False, message="Deletion outside allowed dir."), 403
    try:
        if os.path.isfile(fp) and filename.lower().endswith('.wav'): os.remove(fp); return jsonify(success=True), 200
        else: return jsonify(success=False, message="File not found or not a WAV file."), 404
    except Exception as e: return jsonify(success=False, message=f"Server error: {e}"), 500

@app.route('/rename_audio_file', methods=['POST'])
def rename_audio_file():
    data = request.get_json();
    if not data: return jsonify(success=False, message="No JSON."), 400
    old_fn, new_fn_req = data.get('old_filename'), data.get('new_filename')
    if not old_fn or not new_fn_req: return jsonify(success=False, message="Missing names."), 400
    if any(p in n for n in [old_fn, new_fn_req] for p in ["/", "\\", ".."]): return jsonify(success=False, message="Path chars in names."), 400
    old_b, old_e = os.path.splitext(old_fn); new_b, new_e = os.path.splitext(new_fn_req)
    if not all(e.lower() == '.wav' for e in [old_e, new_e]): return jsonify(success=False, message="Must be .wav."), 400
    new_b_secure = secure_filename(new_b)
    if not new_b_secure: return jsonify(success=False, message="Invalid new base."), 400
    final_new_fn = new_b_secure + ".wav"; old_fp = os.path.join(SOUND_FOLDER, old_fn); new_fp = os.path.join(SOUND_FOLDER, final_new_fn)
    if not os.path.isfile(old_fp): return jsonify(success=False, message=f"'{old_fn}' not found."), 404
    if old_fp == new_fp: return jsonify(success=True, message="Unchanged.", new_filename=old_fn), 200
    if os.path.exists(new_fp): return jsonify(success=False, message=f"'{final_new_fn}' exists."), 409
    try:
        os.rename(old_fp, new_fp); return jsonify(success=True, message="Renamed.", old_filename=old_fn, new_filename=final_new_fn)
    except OSError as e: return jsonify(success=False, message=f"Server error: {e}"), 500

@app.route('/api/speakers', methods=['GET'])
def api_get_speakers():
    discovered_devices, _ , error = discover_and_label_audio_devices()
    if error: return jsonify({"error": str(error)}), 500
    return jsonify([{"id": dev['hw_id'], "name": dev['ha_name']} for dev in discovered_devices])

@app.route('/api/sounds', methods=['GET'])
def api_get_sounds():
    if not os.path.isdir(SOUND_FOLDER): return jsonify({"error": "Sound folder not found"}), 404
    try: return jsonify(sorted([f for f in os.listdir(SOUND_FOLDER) if f.lower().endswith('.wav')]))
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/set_volume/<card_id>/<path:control_name>/<int:volume_percent>', methods=['POST', 'GET'])
def set_device_volume(card_id, control_name, volume_percent):
    if not (0 <= volume_percent <= 100): return jsonify(success=False, message="Volume must be 0-100"), 400
    if not re.fullmatch(r"\d+", card_id): return jsonify(success=False, message="Invalid card_id."), 400
    cmd = [AMIXER_PATH, "-c", card_id, "sset", control_name, f"{volume_percent}%"]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return jsonify(success=True, message="Volume set", output=result.stdout.strip()), 200
    except Exception as e: return jsonify(success=False, message=f"Failed: {e}"), 500

@app.route('/api/card/<card_id>/mixer_controls', methods=['GET'])
def api_get_mixer_controls(card_id):
    if not re.fullmatch(r"\d+", card_id): return jsonify(error="Invalid card_id format."), 400
    try:
        cmd = [AMIXER_PATH, "-c", card_id, "scontrols"]; result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        controls = [re.search(r"Simple mixer control '([^']+)',\d+", line.strip()).group(1) for line in result.stdout.splitlines() if re.search(r"Simple mixer control '([^']+)',\d+", line.strip())]
        return jsonify(controls)
    except Exception as e: return jsonify(error=f"Failed to get mixer controls: {e}"), 500

@app.route('/api/card/<card_id>/control/<path:control_name>/current_volume', methods=['GET'])
def api_get_current_volume(card_id, control_name):
    if not re.fullmatch(r"\d+", card_id): return jsonify(error="Invalid card_id format."), 400
    try:
        cmd = [AMIXER_PATH, "-c", card_id, "sget", control_name]; result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        volume_match = re.search(r"\[(\d+)%\]", result.stdout)
        if volume_match: return jsonify(volume=int(volume_match.group(1)))
        else: return jsonify(volume=None, message="Could not parse volume percentage.")
    except Exception as e: return jsonify(error=f"Failed to get current volume: {e}"), 500

@app.route('/api/get_layout', methods=['GET'])
def api_get_layout(): return jsonify(load_categories())

@app.route('/api/save_layout', methods=['POST'])
def api_save_layout():
    if save_categories(request.get_json()): return jsonify(success=True)
    else: return jsonify(success=False, message="Server error saving layout."), 500

@app.route('/api/speak', methods=['POST'])
def api_speak():
    data = request.get_json(); text_to_speak = data.get('text'); speaker_id = data.get('speaker_id'); lang = data.get('lang', 'en')
    background_sound = data.get('background_sound')
    if not text_to_speak or not speaker_id: return jsonify(success=False, message="Missing 'text' or 'speaker_id'."), 400
    background_path = None
    if background_sound:
        bg_candidate_path = os.path.join(SOUND_FOLDER, background_sound)
        if ".." not in background_sound and os.path.exists(bg_candidate_path): background_path = bg_candidate_path
        else: print(f"Warning: Requested background sound '{background_sound}' not found.")
    print(f"TTS Request: Speak '{text_to_speak}' on device '{speaker_id}' with background '{background_sound}'")
    try:
        tts_filename = f"tts_{uuid.uuid4().hex}.wav"; final_wav_path = os.path.join(TTS_CACHE_FOLDER, tts_filename)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_mp3_path = os.path.join(temp_dir, 'speech.mp3'); tts = gTTS(text_to_speak, lang=lang); tts.save(temp_mp3_path)
            cmd_ffmpeg = [FFMPEG_PATH, '-y', '-i', temp_mp3_path, '-ac', '2', '-ar', '44100', final_wav_path]
            proc = subprocess.run(cmd_ffmpeg, capture_output=True, text=True, check=False)
            if proc.returncode != 0: raise Exception(f"FFMPEG Error: {proc.stderr or proc.stdout}")
        job = {"device_id": speaker_id, "filepath": final_wav_path, "background_path": background_path, "is_temp": True}
        playback_queue.put(job)
        print(f"API: Queued TTS job: {job}")
        return jsonify(success=True, message="Queued TTS playback for processing."), 202
    except Exception as e: print(f"TTS Error: {e}"); return jsonify(success=False, message=str(e)), 500

print("Initializing and starting audio worker thread...")
worker_thread = threading.Thread(target=audio_worker, daemon=True)
worker_thread.start()

if __name__ == '__main__':
    for folder in [SOUND_FOLDER, TTS_CACHE_FOLDER]:
        if not os.path.exists(folder):
            try: os.makedirs(folder, exist_ok=True); print(f"Created folder: {folder}")
            except OSError as e: print(f"Error creating folder {folder}: {e}")
    for f in [DEVICE_LABELS_FILE, CATEGORIES_FILE]:
        if not os.path.exists(f):
            with open(f, 'w') as fh: fh.write('{}')
    print(f"Starting Flask server for development on http://0.0.0.0:{SERVER_PORT}")
    app.run(host='0.0.0.0', port=SERVER_PORT, debug=False, use_reloader=False)