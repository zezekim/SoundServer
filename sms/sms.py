#!/usr/bin/env python3

import serial
import time
import logging
import threading
import re
import csv
import os
import queue
import json
from collections import deque
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template_string, Response
from werkzeug.middleware.proxy_fix import ProxyFix

# Load secrets/config from a .env file (repo root or CWD). See .env.example.
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, '.env'))

# --- Configuration (env-overridable; see .env.example) ---
SERIAL_PORT = os.environ.get('SMS_SERIAL_PORT', '/dev/ttyS0')
BAUD_RATE = int(os.environ.get('SMS_BAUD_RATE', 115200))
WEB_PORT = int(os.environ.get('SMS_WEB_PORT', 5010))
PIN_CODE = os.environ.get('SMS_PIN_CODE') or None
MAX_MESSAGES = 100
CSV_FILE = 'received_sms.csv'
PHONE_NUMBER_MANUAL = os.environ.get('SMS_PHONE_NUMBER') or None
# ---------------------

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# --- Global Objects ---
ser = None
app = Flask(__name__)
# Work both directly (http://pi:5010/) and behind the Caddy proxy (http://pi/sms/).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
serial_lock = threading.Lock()
csv_lock = threading.Lock()
received_messages = deque(maxlen=MAX_MESSAGES)
listener_stop_event = threading.Event()
message_queue = queue.Queue()
CSV_HEADER = ['received_at', 'sender', 'sim_timestamp', 'body']

# --- HTML Template ---
HTML_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">
    <title>SMS Gateway (sms.py)</title>
    <style>
        body { font-family: sans-serif; margin: 20px; background-color: #f4f4f4; }
        .container { max-width: 800px; margin: auto; padding: 20px; border: 1px solid #ccc; border-radius: 8px; background-color: #fff; box-shadow: 2px 2px 10px rgba(0,0,0,0.1); position: relative; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input, textarea { width: 95%; padding: 10px; margin-bottom: 15px; border: 1px solid #ccc; border-radius: 4px; font-size: 1rem; }
        button { padding: 12px 20px; background-color: #28a745; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1rem; }
        button:hover { background-color: #218838; }
        .message { padding: 15px; margin-top: 20px; border-radius: 4px; border: 1px solid transparent; }
        .success { background-color: #d4edda; color: #155724; border-color: #c3e6cb; }
        .error { background-color: #f8d7da; color: #721c24; border-color: #f5c6cb; }
        h1, h2 { border-bottom: 2px solid #eee; padding-bottom: 8px; color: #333; }
        pre { background-color: #e9ecef; padding: 15px; border-radius: 4px; overflow-x: auto; }
        code { color: #c7254e; }
        .sms-list { list-style: none; padding: 0; max-height: 400px; overflow-y: auto; border: 1px solid #ddd; border-radius: 4px; margin-top: 15px; }
        .sms-list li { padding: 10px 15px; border-bottom: 1px solid #eee; }
        .sms-list li:last-child { border-bottom: none; }
        .sms-list .sender { font-weight: bold; color: #0056b3; }
        .sms-list .time { font-size: 0.8em; color: #666; display: block; }
        .sms-list .body { margin-top: 5px; word-wrap: break-word; }
        .status-bar { position: absolute; top: 10px; right: 20px; display: flex; align-items: center; font-size: 0.9em; color: #555; }
        .phone-number { margin-right: 10px; }
        .signal-meter { width: 24px; height: 16px; display: flex; align-items: flex-end; justify-content: space-between; }
        .signal-meter .bar { width: 3px; background-color: #ccc; transition: height 0.3s ease; }
        .signal-meter .bar1 { height: 20%; } .signal-meter .bar2 { height: 40%; } .signal-meter .bar3 { height: 60%; } .signal-meter .bar4 { height: 80%; } .signal-meter .bar5 { height: 100%; }
        .signal-bars-0 .bar { background-color: #e0e0e0; }
        .signal-bars-1 .bar { background-color: #ccc; } .signal-bars-1 .bar1 { background-color: #555; }
        .signal-bars-2 .bar { background-color: #ccc; } .signal-bars-2 .bar1, .signal-bars-2 .bar2 { background-color: #555; }
        .signal-bars-3 .bar { background-color: #ccc; } .signal-bars-3 .bar1, .signal-bars-3 .bar2, .signal-bars-3 .bar3 { background-color: #555; }
        .signal-bars-4 .bar { background-color: #ccc; } .signal-bars-4 .bar1, .signal-bars-4 .bar2, .signal-bars-4 .bar3, .signal-bars-4 .bar4 { background-color: #555; }
        .signal-bars-5 .bar { background-color: #555; }
    </style>
    <script>
        // Mount prefix when served behind the Caddy proxy (e.g. "/sms"); "" when hit directly.
        const BASE = {{ request.script_root | tojson }};
        function escapeHTML(str) { str = str ? String(str) : ''; return str.replace(/[&<>"']/g, function(match) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[match]; }); }
        function addMessageToList(msg, prepend = false) { const list = document.getElementById('smsList'); const placeholder = document.getElementById('smsPlaceholder'); if (placeholder) { list.innerHTML = ''; } const item = document.createElement('li'); item.innerHTML = `<span class="sender">${escapeHTML(msg.sender)}</span><span class="time">SIM: ${escapeHTML(msg.time)} | Rcvd: ${escapeHTML(msg.received_at)}</span><div class="body">${escapeHTML(msg.body)}</div>`; if (prepend) { list.insertBefore(item, list.firstChild); } else { list.appendChild(item); } }
        async function loadInitialMessages() { try { const response = await fetch(BASE + '/api/get_sms'); const messages = await response.json(); const list = document.getElementById('smsList'); list.innerHTML = ''; if (messages.length === 0) { list.innerHTML = '<li id="smsPlaceholder">No messages received yet.</li>'; } else { messages.forEach(msg => addMessageToList(msg, false)); } } catch (error) { console.error("Error fetching messages:", error); document.getElementById('smsList').innerHTML = '<li>Error loading messages.</li>'; } }
        function connectSSE() { console.log("Connecting to SSE stream..."); const eventSource = new EventSource(BASE + "/stream"); eventSource.onmessage = function(event) { console.log("SSE Message Received:", event.data); try { const msg = JSON.parse(event.data); addMessageToList(msg, true); } catch(e) { console.error("Failed to parse SSE data:", e); } }; eventSource.onerror = function(err) { console.error("EventSource failed:", err); eventSource.close(); setTimeout(connectSSE, 5000); }; eventSource.onopen = function() { console.log("SSE Connection opened."); }; }
        async function fetchStatus() { try { const response = await fetch(BASE + '/api/status'); const status = await response.json(); document.getElementById('phoneNumber').textContent = escapeHTML(status.number || 'Unknown'); document.getElementById('signalMeter').className = 'signal-meter signal-bars-' + (status.signal_bars || 0); } catch (error) { console.error("Error fetching status:", error); document.getElementById('phoneNumber').textContent = 'Error'; document.getElementById('signalMeter').className = 'signal-meter signal-bars-0'; } }
        window.onload = function() { loadInitialMessages(); connectSSE(); fetchStatus(); setInterval(fetchStatus, 15000); };
    </script>
</head>
<body>
    <div class="container">
        <div class="status-bar"> <span id="phoneNumber" class="phone-number">Loading...</span> <div id="signalMeter" class="signal-meter signal-bars-0"> <div class="bar bar1"></div> <div class="bar bar2"></div> <div class="bar bar3"></div> <div class="bar bar4"></div> <div class="bar bar5"></div> </div> </div>
        <h1>Raspberry Pi SMS Gateway</h1>
         {% if message %} <div class="message {{ 'success' if success else 'error' }}">{{ message }}</div> {% endif %}
        <div style="display: flex; gap: 20px;">
            <div style="flex: 1;"> <h2>Send SMS</h2> <form method="post" action="{{ request.script_root }}/send"> <label for="number">Phone Number (Intl. Format):</label> <input type="tel" id="number" name="number" placeholder="+1234567890" required> <label for="text">Message:</label> <textarea id="text" name="text" rows="4" maxlength="160" required></textarea> <button type="submit">Send SMS</button> </form> </div>
            <div style="flex: 1;"> <h2>Received SMS (Real-time)</h2> <ul id="smsList" class="sms-list"> <li>Loading messages...</li> </ul> </div>
        </div>
        <h2>API Info</h2>
        <p>Send a POST request to <code>{{ request.script_root }}/api/send_sms</code> with JSON data:</p> <pre>{ "number": "+1234567890", "message": "Your alert!" }</pre>
        <p>Get received SMS via GET request to <code>{{ request.script_root }}/api/get_sms</code>.</p> <p>Listen for new SMS at <code>{{ request.script_root }}/stream</code> (SSE).</p>
        <p>SMS messages are logged to <code>{{ csv_file_path }}</code>.</p>
    </div>
</body>
</html>
"""

# --- CSV Functions ---
def log_sms_to_csv(message_data):
    with csv_lock:
        file_exists = os.path.isfile(CSV_FILE)
        try:
            with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
                if not file_exists: writer.writeheader(); logging.info(f"Created CSV file: {CSV_FILE}")
                writer.writerow(message_data); logging.info(f"Logged SMS to {CSV_FILE}")
        except Exception as e: logging.error(f"Error writing to CSV file {CSV_FILE}: {e}")

def load_sms_from_csv():
    with csv_lock:
        if not os.path.isfile(CSV_FILE): logging.info("CSV file not found."); return
        try:
            with open(CSV_FILE, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f); all_lines = list(reader)
                start_index = max(0, len(all_lines) - MAX_MESSAGES); loaded_count = 0
                for row in all_lines[start_index:]:
                    row_data = {"received_at": row.get('received_at'), "sender": row.get('sender'), "time": row.get('sim_timestamp'), "body": row.get('body')}
                    received_messages.append(row_data); loaded_count += 1
                logging.info(f"Loaded {loaded_count} messages from {CSV_FILE}.")
        except Exception as e: logging.error(f"Error reading from CSV file {CSV_FILE}: {e}")

# --- AT Command Functions ---
def send_at_command(command, expected_response="OK", timeout=5, return_lines=False):
    with serial_lock:
        if not ser or not ser.is_open: logging.error("Serial port not open."); return None
        try:
            logging.info(f"Sending: {command}"); ser.reset_input_buffer(); ser.write((command + '\r\n').encode('utf-8'))
            response_lines = []; start_time = time.time()
            while time.time() - start_time < timeout:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if line:
                    logging.info(f"Received: {line}"); response_lines.append(line)
                    if expected_response in line: return response_lines if return_lines else True
                    if "ERROR" in line or "FAIL" in line: logging.error(f"Command '{command}' failed: {response_lines}"); return None
            logging.warning(f"Timeout ({timeout}s) waiting for '{expected_response}' for command '{command}'")
            return response_lines if return_lines and response_lines else None
        except Exception as e: logging.error(f"Error sending AT command: {e}"); return None

# --- Status Functions ---
def get_signal_strength():
    lines = send_at_command("AT+CSQ", "+CSQ:", timeout=5, return_lines=True)
    if not lines: return 0, 99
    match = re.search(r'\+CSQ: (\d+),(\d+)', lines[0])
    if match:
        rssi = int(match.group(1)); ber = int(match.group(2))
        logging.info(f"Signal Strength RSSI: {rssi}, BER: {ber}")
        if rssi == 99: return 0, rssi
        if rssi <= 1: return 0, rssi;
        if rssi <= 9: return 1, rssi;
        if rssi <= 14: return 2, rssi;
        if rssi <= 19: return 3, rssi;
        if rssi <= 30: return 4, rssi;
        if rssi >= 31: return 5, rssi
    return 0, 99

def get_phone_number():
    if PHONE_NUMBER_MANUAL: return PHONE_NUMBER_MANUAL
    lines = send_at_command("AT+CNUM", "+CNUM:", timeout=10, return_lines=True)
    if not lines: return "Unknown"
    match = re.search(r'\+CNUM:.*,"(\+?\d+)"', lines[0])
    if match: return match.group(1)
    else: logging.warning("Could not get phone number from SIM (AT+CNUM)."); return "Not Set on SIM"

# --- SMS Processing ---
def read_sms(index):
    logging.info(f"Attempting to read SMS at index {index}...")
    message_data = None
    with serial_lock:
        if not ser or not ser.is_open: return None
        try:
            ser.reset_input_buffer(); ser.write((f"AT+CMGR={index}\r\n").encode('utf-8'))
            lines = []; header_found = False; body_started = False; body_lines = []; sender = ""; sim_timestamp = ""
            start_time = time.time()
            while time.time() - start_time < 15:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if not line: continue
                logging.debug(f"CMGR Read: {line}"); lines.append(line)
                if line.startswith("+CMGR:"):
                    header_found = True
                    header_match = re.search(r'\+CMGR:.*,"(.*?)",.*,"(.*?)"', line)
                    if header_match: sender = header_match.group(1); sim_timestamp = header_match.group(2); body_started = True
                    else: logging.error(f"Could not parse +CMGR header: {line}"); break
                elif body_started and line != "OK" and not line.startswith("ERROR"): body_lines.append(line)
                elif line == "OK":
                    if header_found:
                        body = "\n".join(body_lines); received_at_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        message_data = {"received_at": received_at_ts, "sender": sender, "sim_timestamp": sim_timestamp, "body": body}
                        logging.info(f"Successfully Parsed SMS: {sender} - {body[:30]}...")
                    break
                elif "ERROR" in line: logging.error(f"Error reading SMS at index {index}: {lines}"); break
            if not message_data: logging.error(f"Failed to fully parse/read SMS at index {index}. Raw lines: {lines}")
            logging.info(f"Attempting to delete SMS at index {index}..."); ser.write((f"AT+CMGD={index}\r\n").encode('utf-8'))
            del_time = time.time()
            while time.time() - del_time < 10:
                del_line = ser.readline().decode('utf-8', errors='ignore').strip()
                if del_line == "OK": logging.info(f"Deleted SMS at index {index}."); break
                elif "ERROR" in del_line: logging.warning(f"Failed to delete SMS at index {index}. Error: {del_line}"); break
            else: logging.warning(f"Timeout deleting SMS at index {index}.")
        except Exception as e: logging.error(f"Exception during read_sms at index {index}: {e}"); return None
    if message_data:
        web_info = {"received_at": message_data["received_at"], "sender": message_data["sender"], "time": message_data["sim_timestamp"], "body": message_data["body"]}
        received_messages.appendleft(web_info); log_sms_to_csv(message_data)
        try: message_queue.put(web_info); logging.info("Message added to SSE queue.")
        except Exception as e: logging.error(f"Failed to add message to SSE queue: {e}")
    return message_data

# --- Listener, Setup, Network Check ---
def sms_listener_thread():
    logging.info("SMS Listener thread started.")
    while not listener_stop_event.is_set():
        try:
            if ser and ser.is_open:
                message_index_to_read = None
                with serial_lock:
                    while ser.in_waiting > 0:
                        line = ser.readline().decode('utf-8', errors='ignore').strip()
                        if line:
                            logging.debug(f"Listener received: {line}")
                            match = re.search(r'\+CMTI:.*?,(\d+)', line)
                            if match: message_index_to_read = match.group(1); logging.info(f"New SMS notification, index: {message_index_to_read}"); break
                if message_index_to_read: read_sms(message_index_to_read)
                time.sleep(0.2)
            else: logging.warning("Listener: Serial port not open. Waiting..."); time.sleep(5)
        except Exception as e: logging.error(f"Error in SMS listener thread: {e}"); time.sleep(5)
    logging.info("SMS Listener thread stopped.")

def check_network():
    logging.info("Checking network registration...")
    for i in range(15):
        lines = send_at_command("AT+CREG?", expected_response="+CREG:", timeout=3, return_lines=True)
        if lines and isinstance(lines, list) and any(val in line for line in lines for val in [',1', ',5']):
            logging.info("Network registration successful."); return True
        logging.info(f"Waiting for network registration... ({i+1}/15)"); time.sleep(2)
    logging.error("Failed to register on network."); return False

def setup_sim800l():
    global ser
    try:
        logging.info(f"Opening serial port {SERIAL_PORT} at {BAUD_RATE}..."); ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1); time.sleep(1)
        if not ser.is_open: return False
        if not send_at_command("AT"): return False
        if not send_at_command("ATE0"): return False
        if not send_at_command("AT+CPIN?", "+CPIN: READY"):
             if PIN_CODE:
                 if not send_at_command(f'AT+CPIN="{PIN_CODE}"', "OK", 10): return False
             else: logging.error("SIM not ready."); return False
        if not check_network(): return False
        if not send_at_command("AT+CMGF=1"): return False
        if not send_at_command("AT+CNMI=2,1,0,0,0"): return False
        logging.info("Deleting existing messages..."); send_at_command('AT+CMGD=1,4', "OK", 20)
        logging.info("SIM800L setup successful!"); return True
    except Exception as e: logging.error(f"Setup error: {e}"); close_serial(); return False

# --- Send SMS ---
def send_sms(number, message):
    num_to_send = str(number)
    if not num_to_send.startswith('+'):
        logging.warning(f"Number {num_to_send} missing '+'. Assuming 63 and adding.")
        if num_to_send.startswith('63'): num_to_send = f"+{num_to_send}"
        else: logging.warning(f"Unsure of number format, adding '+' anyway."); num_to_send = f"+{num_to_send}"
    if not send_at_command("AT"):
         logging.error("Module not responding before send. Trying full setup.")
         if not setup_sim800l(): logging.error("Cannot send SMS, module not ready."); return False
    logging.info(f"Attempting to send SMS to {num_to_send}")
    if not send_at_command("AT+CMGF=1"): logging.error("Failed to set SMS text mode before sending."); return False
    with serial_lock:
        if not ser or not ser.is_open: return False
        ser.reset_input_buffer(); ser.write((f'AT+CMGS="{num_to_send}"\r\n').encode('utf-8'))
        start_time = time.time(); got_prompt = False
        while time.time() - start_time < 15:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line: logging.info(f"Received: {line}")
            if ">" in line: got_prompt = True; break
            if "ERROR" in line: logging.error("CMGS Error"); return False
        if not got_prompt: logging.error("CMGS Timeout/Error"); return False
        logging.info(f"Sending message body..."); ser.write(message.encode('utf-8')); ser.write(b'\x1a')
        start_time = time.time()
        while time.time() - start_time < 90:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line:
                logging.info(f"Received: {line}")
                if "+CMGS:" in line: logging.info("SMS sent!"); return True
                if "ERROR" in line: logging.error(f"SMS send ERROR: {line}"); return False
    logging.error("SMS send timeout."); return False

def close_serial():
    global ser
    if ser and ser.is_open: logging.info("Closing serial port."); ser.close(); ser = None

# --- Flask App ---
@app.route('/')
def index(): return render_template_string(HTML_TEMPLATE, csv_file_path=CSV_FILE)
@app.route('/send', methods=['POST'])
def web_send():
    number = request.form.get('number'); text = request.form.get('text'); msg = None; success_status = False
    if not number or not text: msg = "Phone number and message are required."
    elif send_sms(number, text): msg = f"SMS sent to {number}!"; success_status = True
    else: msg = "Failed to send SMS. Check logs for details."
    return render_template_string(HTML_TEMPLATE, message=msg, success=success_status, csv_file_path=CSV_FILE)

@app.route('/api/send_sms', methods=['POST'])
def api_send():
    if not request.is_json: return jsonify({"status": "error", "message": "Request must be JSON"}), 400
    data = request.get_json(); numbers = data.get('number'); message = data.get('message')
    if not message: return jsonify({"status": "error", "message": "Missing 'message'"}), 400
    if not numbers: return jsonify({"status": "error", "message": "Missing 'number'"}), 400
    if isinstance(numbers, list):
        results = {}; all_ok = True
        logging.info(f"Received request to send to multiple numbers: {len(numbers)}")
        for num in numbers:
            num_str = str(num)
            if send_sms(num_str, message): results[num_str] = "success"
            else: results[num_str] = "failed"; all_ok = False
            time.sleep(3)
        return jsonify({"status": "multiple_results", "results": results, "overall_ok": all_ok }), 200 if all_ok else 207
    else:
        num_str = str(numbers)
        if send_sms(num_str, message): return jsonify({"status": "success", "message": f"SMS sent to {num_str}"}), 200
        else: return jsonify({"status": "error", "message": f"Failed to send SMS to {num_str}"}), 500

# ****** THIS IS THE MISSING ROUTE THAT WAS RE-ADDED ******
@app.route('/api/get_sms', methods=['GET'])
def api_get_sms():
    """Returns the list of received messages from the in-memory deque."""
    return jsonify(list(received_messages))
# ****** END OF RE-ADDED ROUTE ******

@app.route('/api/status')
def api_status():
    bars, rssi = get_signal_strength(); number = get_phone_number()
    return jsonify({"signal_bars": bars, "signal_rssi": rssi, "number": number})

@app.route('/stream')
def stream():
    def event_stream():
        logging.info("SSE Client connected.")
        try:
            while True: msg = message_queue.get(); logging.info(f"SSE: Sending message to client: {msg['sender']}"); yield f"data: {json.dumps(msg)}\n\n"
        except GeneratorExit: logging.info("SSE Client disconnected.")
        except Exception as e: logging.error(f"Error in SSE stream: {e}")
    return Response(event_stream(), mimetype='text/event-stream')

# --- Main Execution ---
if __name__ == '__main__':
    listener_thread_obj = None
    logging.info("Starting SMS Gateway script...")
    load_sms_from_csv()
    if setup_sim800l():
        listener_stop_event.clear()
        listener_thread_obj = threading.Thread(target=sms_listener_thread, daemon=True)
        listener_thread_obj.start()
        logging.info(f"Starting web server on port {WEB_PORT}...")
        try: app.run(host='0.0.0.0', port=WEB_PORT, debug=False, threaded=True)
        except Exception as e: logging.error(f"Web server error: {e}")
        finally:
            logging.info("Shutting down..."); listener_stop_event.set()
            if listener_thread_obj: listener_thread_obj.join(timeout=5)
            close_serial()
    else: logging.error("SIM800L setup failed. Exiting."); close_serial()