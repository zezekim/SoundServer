#!/usr/bin/env python3
import serial
import time
import subprocess
import os
import re
import signal
import threading
import json
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

# Load secrets/config from a .env file (repo root or CWD). See .env.example.
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, '.env'))

app = Flask(__name__)
app.secret_key = os.environ.get('CALL_FLASK_SECRET_KEY') or os.urandom(32).hex()

# --- Configuration (env-overridable; see .env.example) ---
SERIAL_PORT = os.environ.get('CALL_SERIAL_PORT', "/dev/ttyS0")
BAUD_RATE = int(os.environ.get('CALL_BAUD_RATE', 115200))
WEB_PORT = int(os.environ.get('CALL_WEB_PORT', 5020))
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "call_config.json")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CHIME_WAV_PATH = os.path.join(PROJECT_DIR, "chime.wav")

# --- Global Variables ---
config = {}
config_lock = threading.RLock()
ser = None
is_call_active = False
current_caller_id = None
audio_process = None
is_ringing = False
ring_start_time = 0

# --- AT Command Definitions ---
CMD_ECHO_OFF = "ATE0\r\n"
CMD_ENABLE_VERBOSE_ERRORS = "AT+CMEE=2\r\n"
CMD_CHECK_SIM = "AT+CPIN?\r\n"
CMD_CHECK_REGISTRATION = "AT+CREG?\r\n"
CMD_ENABLE_CALLER_ID = "AT+CLIP=1\r\n"
CMD_ANSWER_CALL = "ATA\r\n"
CMD_HANGUP_CALL = "ATH\r\n"
CMD_CHECK_CALL_STATUS = "AT+CLCC\r\n"

# --- Configuration Management ---
def load_config():
    global config
    defaults = {
        "card_labels": {"1": "Indoor Speaker", "2": "SIM Audio Input", "3": "Outdoor Speaker"},
        "chime": {"card_id": "1", "control": "Speaker", "volume": 75},
        "call_broadcast": {"card_id": "3", "control": "Speaker", "volume": 100},
        "call_input_card_id": "2",
        "answer_delay_seconds": 5
    }
    with config_lock:
        loaded_config = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    loaded_config = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                print("Config file found but invalid. Creating with full defaults.")
        else:
            print("Config file not found. Creating with full defaults.")
        
        config = defaults.copy()
        if isinstance(loaded_config, dict):
            for key, value in loaded_config.items():
                if isinstance(value, dict) and key in config and isinstance(config[key], dict):
                    config[key].update(value)
                else:
                    config[key] = value
        save_config()
    print(f"Configuration loaded: {config}")

def save_config():
    with config_lock:
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=4)
            print("Configuration saved.")
        except Exception as e:
            print(f"ERROR: Could not save configuration to {CONFIG_FILE}: {e}")

# --- Audio & Modem Functions ---
def list_all_cards():
    cards = {}
    try:
        with open('/proc/asound/cards', 'r') as f:
            for line in f:
                match = re.match(r'^\s*(\d+)\s*\[\s*([^\]]+)\s*\]:\s*(.*)', line)
                if match:
                    card_id, _, name = match.groups()
                    cards[card_id] = {"id": card_id, "name": name.strip(), "is_output": False, "is_input": False}
        aplay_out = subprocess.check_output(["/usr/bin/aplay", "-l"], text=True)
        for line in aplay_out.splitlines():
            if line.strip().startswith("card"):
                card_num = line.split(' ')[1].strip(':')
                if card_num in cards: cards[card_num]["is_output"] = True
        arecord_out = subprocess.check_output(["/usr/bin/arecord", "-l"], text=True)
        for line in arecord_out.splitlines():
            if line.strip().startswith("card"):
                card_num = line.split(' ')[1].strip(':')
                if card_num in cards: cards[card_num]["is_input"] = True
        with config_lock:
            card_labels = config.get("card_labels", {})
        for card_id, card_data in cards.items():
            card_data["label"] = card_labels.get(card_id, "")
    except Exception as e: print(f"Error listing sound cards: {e}")
    return sorted(list(cards.values()), key=lambda x: int(x['id']))

def set_volume_for_card(card_id, control_name, volume_percent):
    if not card_id or not control_name:
        msg = f"Volume not set: Invalid card_id ('{card_id}') or control_name ('{control_name}') provided."
        print(msg); return False, msg
    try:
        cmd = ["/usr/bin/amixer", "-c", str(card_id), "sset", f"'{control_name}'", f"{volume_percent}%", "unmute"]
        print(f"Setting volume: {' '.join(cmd)}")
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True, f"Volume set to {volume_percent}%"
    except Exception as e: msg = f"Error setting volume for card {card_id}: {e}"; print(msg); return False, msg

def play_sound_on_device(device_id, sound_path):
    if not device_id or "None" in str(device_id):
        msg = f"Playback failed: Invalid device_id ('{device_id}') provided."; print(msg); return False, msg
    print(f"Attempting to play sound: {sound_path} on ALSA device {device_id}")
    if not os.path.exists(sound_path):
        msg = f"Sound file not found: {os.path.basename(sound_path)}"; print(msg); return False, msg
    try:
        cmd = ["/usr/bin/aplay", "-D", device_id, sound_path]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        print(f"Sound played successfully on {device_id}."); return True, "Playback successful."
    except subprocess.CalledProcessError as e: msg = f"Error playing sound with aplay: {e.stderr}"; print(msg); return False, msg
    except Exception as e: msg = f"An unexpected error occurred: {e}"; print(msg); return False, msg

def play_chime_locally():
    with config_lock:
        cfg = config.get("chime", {}); card_id = cfg.get('card_id')
        device_id = f"hw:{card_id},0" if card_id is not None else None
        control, volume = cfg.get('control'), cfg.get('volume')
    if not all([card_id, device_id, control, volume is not None]):
        print("Chime configuration is incomplete. Skipping chime."); return
    set_volume_for_card(card_id, control, volume)
    play_sound_on_device(device_id, CHIME_WAV_PATH)

def setup_live_audio_broadcast_alsa():
    global audio_process
    with config_lock:
        input_card_id = config.get("call_input_card_id")
        output_cfg = config.get("call_broadcast", {}); output_card_id = output_cfg.get("card_id")
        output_control, output_volume = output_cfg.get("control"), output_cfg.get("volume")
    if not all([input_card_id, output_card_id, output_control, output_volume is not None]):
        print("Broadcast configuration is incomplete. Skipping audio routing."); return
    set_volume_for_card(output_card_id, output_control, output_volume)
    input_device, output_device = f"hw:{input_card_id},0", f"hw:{output_card_id},0"
    print(f"Starting audio pipeline from {input_device} to {output_device}...")
    command_str = (f"/usr/bin/arecord -D {input_device} -r 44100 -f S16_LE -c 1 -t raw | "
                   f"/usr/bin/sox -t raw -r 44100 -e signed-integer -b 16 -c 1 - -t raw -c 2 - | "
                   f"/usr/bin/aplay -D {output_device} -f S16_LE -c 2 -r 44100")
    try:
        print(f"Running command pipeline: {command_str}")
        audio_process = subprocess.Popen(command_str, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
        time.sleep(1)
        if audio_process.poll() is None: print(f"Audio pipeline started with PGID: {audio_process.pid}")
        else: print(f"Audio pipeline failed to start. Exit code: {audio_process.poll()}"); audio_process = None
    except Exception as e: print(f"Error starting audio pipeline: {e}"); audio_process = None

def takedown_live_audio_broadcast_alsa():
    global audio_process
    if audio_process and audio_process.poll() is None:
        print(f"Stopping audio pipeline with PGID: {audio_process.pid}...")
        try:
            os.killpg(os.getpgid(audio_process.pid), signal.SIGTERM); audio_process.wait(timeout=2); print("Audio pipeline terminated.")
        except Exception as e:
            print(f"Error stopping audio pipeline process: {e}")
            try: audio_process.kill(); audio_process.wait()
            except Exception as e2: print(f"Error force-killing pipeline: {e2}")
        finally: audio_process = None
    else: print("No active audio pipeline to stop.")

def start_modem_listener():
    print("Background modem listener thread started.")
    if setup_serial():
        if initialize_modem(): main_event_loop()
    print("Modem listener thread has ended.")
def setup_serial():
    global ser
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1); print(f"Serial port {SERIAL_PORT} opened."); return True
    except serial.SerialException as e: print(f"Error opening serial port {SERIAL_PORT}: {e}"); ser = None; return False
def send_at_command(command, success_markers=("OK",), error_markers=("ERROR",), data_markers=(), timeout=5):
    if not ser or not ser.is_open: print("Serial port not open."); return False, []
    ser.reset_input_buffer(); print(f"AT CMD TX: {command.strip()}"); ser.write(command.encode('utf-8'))
    lines, start_time = [], time.time()
    while time.time() - start_time < timeout:
        try:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line:
                print(f"AT CMD RX: {line}"); lines.append(line)
                if any(m in line for m in success_markers) or any(m in line for m in error_markers): break
        except Exception as e: print(f"Error reading from serial port: {e}"); return False, lines
    is_ok = any(m in line for line in lines for m in success_markers)
    is_err = any(m in line for line in lines for m in error_markers)
    if data_markers: is_ok = is_ok and any(m in line for line in lines for m in data_markers)
    if not is_ok and not is_err: print(f"AT Command TIMEOUT for: {command.strip()}")
    return is_ok, lines
def initialize_modem():
    print("Initializing modem..."); send_at_command(CMD_ECHO_OFF); time.sleep(0.2)
    send_at_command(CMD_ENABLE_VERBOSE_ERRORS); time.sleep(0.2)
    sim_ok, _ = send_at_command(CMD_CHECK_SIM, data_markers=("+CPIN: READY",))
    if not sim_ok: print("SIM Card Error or not ready."); return False
    time.sleep(0.2); send_at_command(CMD_ENABLE_CALLER_ID); time.sleep(0.2)
    print("Checking network registration...")
    for _ in range(15):
        reg_ok, r_lines = send_at_command(CMD_CHECK_REGISTRATION, data_markers=("+CREG:",))
        if reg_ok and any(("+CREG: 0,1" in l or "+CREG: 0,5" in l) for l in r_lines):
            print("Modem registered on network."); print("Modem initialization complete."); return True
        print("Not registered yet, waiting..."); time.sleep(2)
    print("Modem failed to register on network."); return False
def answer_incoming_call():
    global is_call_active, current_caller_id
    print("Answering incoming call..."); send_at_command(CMD_ANSWER_CALL, success_markers=("OK", "CONNECT"), timeout=10)
    clcc_ok, r_lines = send_at_command(CMD_CHECK_CALL_STATUS, data_markers=("+CLCC:",))
    call_active = False
    if clcc_ok and r_lines:
        for line in r_lines:
            if "+CLCC:" in line:
                parts = line.replace("+CLCC: ", "").split(',');
                if len(parts) >= 4 and parts[2] == '0' and parts[3] == '0': call_active = True; break
    if call_active:
        is_call_active = True; print(f"Call is active with {current_caller_id or 'unknown caller'}."); setup_live_audio_broadcast_alsa()
    else:
        print("Could not confirm active call. Hanging up."); send_at_command(CMD_HANGUP_CALL); is_call_active = False; current_caller_id = None
def hangup_current_call():
    global is_call_active, current_caller_id
    if is_call_active:
        print("Hanging up active call..."); takedown_live_audio_broadcast_alsa(); send_at_command(CMD_HANGUP_CALL)
        is_call_active = False; current_caller_id = None; print("Call hung up.")
        print("Pinging modem with AT to confirm communication is alive...")
        time.sleep(1)
        success, _ = send_at_command("AT\r\n")
        if success: print("Modem responded to post-call AT ping. Ready for next call.")
        else: print("WARNING: Modem did not respond to post-call AT ping.")
def main_event_loop():
    global ser, is_call_active, current_caller_id, is_ringing, ring_start_time
    is_ringing, ring_start_time = False, 0
    print("Starting main event loop to monitor SIM800L...")
    while True:
        if is_ringing and not is_call_active:
            with config_lock: answer_delay = int(config.get("answer_delay_seconds", 5))
            if time.time() - ring_start_time >= answer_delay:
                print(f"{answer_delay}s passed since first RING. Answering call.")
                answer_incoming_call(); is_ringing = False
        if not ser or not ser.is_open:
            print("Serial port is closed. Reconnecting...");
            if setup_serial(): initialize_modem()
            else: time.sleep(10); continue
        try:
            modem_response_line = ser.readline().decode('utf-8', errors='ignore').strip()
            if modem_response_line:
                print(f"Modem Raw RX: {modem_response_line}")
                if "RING" in modem_response_line and not is_call_active and not is_ringing:
                    print("First RING detected. Starting answer countdown.")
                    is_ringing, ring_start_time = True, time.time(); play_chime_locally()
                elif "+CLIP:" in modem_response_line:
                    match = re.search(r'\+CLIP:\s*"([^"]*)"', modem_response_line)
                    if match: current_caller_id = match.group(1); print(f"Caller ID received: {current_caller_id}")
                elif any(term in modem_response_line for term in ["NO CARRIER", "BUSY", "NO ANSWER", "VOICE CALL: END"]):
                    if is_call_active or is_ringing:
                        print(f"Call termination detected: {modem_response_line}")
                        if is_call_active: hangup_current_call()
                        is_ringing, current_caller_id = False, None
        except serial.SerialException as e:
            print(f"Serial exception: {e}"); ser = None; time.sleep(5)
        except KeyboardInterrupt: print("Exiting by user request (Ctrl+C)..."); break
        except Exception as e:
            print(f"Unexpected error in loop: {e}"); import traceback; traceback.print_exc(); time.sleep(5) 
        time.sleep(0.1)

# --- Flask Web Server ---
@app.route('/')
def index():
    all_cards = list_all_cards()
    with config_lock:
        current_config_for_template = json.loads(json.dumps(config))
    return render_template('index.html', all_cards=all_cards, config=current_config_for_template)

@app.route('/save_labels', methods=['POST'])
def save_labels_route():
    with config_lock:
        if "card_labels" not in config: config["card_labels"] = {}
        for key, value in request.form.items():
            if key.startswith("label_"):
                card_id = key.split("_")[1]; config["card_labels"][card_id] = value.strip()
        save_config()
    flash('Card labels saved successfully!'); return redirect(url_for('index'))

@app.route('/save_config', methods=['POST'])
def save_config_route():
    with config_lock:
        # Update config, providing existing values as fallback if form field is missing
        config["answer_delay_seconds"] = int(request.form.get("answer_delay", config.get("answer_delay_seconds", 5)))
        config["call_input_card_id"] = request.form.get("call_input_card", config.get("call_input_card_id"))
        
        if "chime" not in config: config["chime"] = {}
        config["chime"]["card_id"] = request.form.get("chime_card")
        config["chime"]["control"] = request.form.get("chime_control", "").strip()
        config["chime"]["volume"] = int(request.form.get("chime_volume"))
        
        if "call_broadcast" not in config: config["call_broadcast"] = {}
        config["call_broadcast"]["card_id"] = request.form.get("call_card")
        config["call_broadcast"]["control"] = request.form.get("call_control", "").strip()
        config["call_broadcast"]["volume"] = int(request.form.get("call_volume"))
        
        save_config()
    flash('Configuration saved successfully!'); return redirect(url_for('index'))
    
@app.route('/test_play/<string:card_id>/<int:volume>')
def test_play(card_id, volume):
    """Plays chime on a device, using configured control name or trying defaults."""
    device_id = f"hw:{card_id},0"
    control_name_to_use = None
    
    with config_lock:
        # Check if the tested card is one of the configured roles to get its specific control name
        if config.get("chime", {}).get("card_id") == card_id:
            control_name_to_use = config["chime"].get("control")
        elif config.get("call_broadcast", {}).get("card_id") == card_id:
            control_name_to_use = config["call_broadcast"].get("control")

    # If no specific control is configured for this card, try a list of common names
    if not control_name_to_use:
        print(f"No specific mixer control configured for card {card_id}. Trying defaults...")
        controls_to_try = ['Speaker', 'PCM', 'Master', 'Front']
        for control in controls_to_try:
            success, _ = set_volume_for_card(card_id, control, volume)
            if success:
                control_name_to_use = control; break
    else:
        # We have a configured control name, use it directly
        success, _ = set_volume_for_card(card_id, control_name_to_use, volume)
        if not success:
            return jsonify(success=False, error=f"Failed to set volume using configured control '{control_name_to_use}'."), 500
    
    if not control_name_to_use:
        return jsonify(success=False, error="Could not set volume using common control names."), 500
    
    # Now play the sound
    success, message = play_sound_on_device(device_id, CHIME_WAV_PATH)
    return jsonify(success=success, error=message if not success else None), 200 if success else 500

if __name__ == "__main__":
    load_config()
    print("Creating and starting modem listener thread...")
    modem_thread = threading.Thread(target=start_modem_listener, daemon=True)
    modem_thread.start()
    print("Modem listener thread started. Starting Flask web server...")
    app.run(host='0.0.0.0', port=WEB_PORT, debug=False)