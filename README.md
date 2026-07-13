# SoundServer

A collection of Raspberry Pi services that turn a Pi + a SIM800L GSM module + a
set of USB/HDMI sound cards into a home audio, intercom, and alerting hub. It is
designed to be driven by Home Assistant (or any HTTP client) and is used for
things like security announcements, weather/UV updates, welcome messages, a
GSM auto-answer intercom, an SMS gateway, and network-uptime chimes.

The project is made up of four independent services, each in its own directory.

| Service | Directory | Port | What it does |
|---------|-----------|------|--------------|
| **Sound Server** | `flask-env/` | `5000` | Web UI + REST API to play WAV files and TTS on any ALSA speaker, with mixing, volume control, upload/convert, and a drag-and-drop soundboard. |
| **Call Intercom** | `call/` | `5020` | Auto-answers incoming GSM calls on a SIM800L, plays a chime, and broadcasts the live call audio to an outdoor speaker. Web UI for configuration. |
| **SMS Gateway** | `sms/` | `5010` | Send/receive SMS over the SIM800L via web UI, REST API, and a live SSE feed. Logs everything to CSV. |
| **Uptime Monitor** | `uptime/` | — | Pings a target host and plays a "connected"/"disconnected" WAV chime when the link state changes. No web UI. |

The `sound/` directory holds the master library of source `.mp3` prompts (person
detection, weather, welcome messages, sirens, etc.) plus a helper script that
pads and converts them to the `.wav` files the Sound Server plays.

---

## Components

### 1. Sound Server — `flask-env/app.py` (port 5000)

The main service. A Flask app that discovers every ALSA playback device
(`aplay -l`) and lets you play sounds to any of them.

**Features**
- Play a WAV file to a specific `hw:card,device` speaker, with optional repeat count.
- Overlay a foreground sound on top of a background bed (mixed with `pydub`).
- Text-to-speech via `gTTS`, rendered to WAV with `ffmpeg` and queued for playback.
- A single background **audio worker thread** consumes a playback queue so requests
  never block and playbacks are serialized per device.
- Upload `.wav`/`.mp3` (auto-converted with `ffmpeg`), rename, and delete sounds.
- Per-card volume control and mixer-control discovery via `amixer`.
- A soundboard UI with user-editable device labels, categories, and tags
  (`devices.json`, `categories.json`, `tags.json`).

**Key HTTP API**
- `GET  /play/<device_id>/<filename>[/<count>]` — queue playback (`?background=<file>` to mix).
- `POST /api/speak` — `{ "text", "speaker_id", "lang", "background_sound" }` → TTS.
- `GET  /api/speakers` — list discovered speakers.
- `GET  /api/sounds` — list available WAV files.
- `POST /api/set_volume/<card_id>/<control>/<volume_percent>` — set mixer volume.
- `GET/POST /api/get_layout` · `/api/save_layout` — soundboard categories.
- `POST /upload` · `/delete/<file>` · `/rename_audio_file` — library management.

Runs standalone (`app.run(... port=5000)`) or under **gunicorn** using
`gunicorn_config.py`, whose `post_fork` hook starts the audio worker in each worker.

### 2. Call Intercom — `call/call.py` (port 5020)

Listens on the SIM800L serial port for incoming calls. On the first `RING` it
plays a local chime and, after a configurable delay, sends `ATA` to answer. Once
the call is confirmed active (`AT+CLCC`), it opens a live audio pipeline —
`arecord | sox | aplay` — that routes the modem's audio input to an outdoor
broadcast speaker. Call teardown (`NO CARRIER`, `BUSY`, `VOICE CALL: END`) tears
the pipeline down and hangs up.

Configuration (which sound cards are chime / input / broadcast, volumes, answer
delay) is stored in `call/call_config.json` and editable from the web UI at
`http://<pi>:5020/`.

### 3. SMS Gateway — `sms/sms.py` (port 5010)

Initializes the SIM800L in text mode (`AT+CMGF=1`), registers on the network,
and listens for `+CMTI` new-message notifications. Incoming messages are parsed
(`AT+CMGR`), deleted from the SIM, appended to `received_sms.csv`, and pushed to
connected browsers over Server-Sent Events. The UI also shows live signal
strength (`AT+CSQ`) and the SIM's own number (`AT+CNUM`).

**HTTP API**
- `POST /api/send_sms` — `{ "number": "+63...", "message": "..." }` (also accepts a
  list of numbers).
- `GET  /api/get_sms` — recent received messages.
- `GET  /api/status` — signal bars + phone number.
- `GET  /stream` — SSE feed of new messages.

> **Note:** the Call Intercom and SMS Gateway both talk to the *same* SIM800L on
> `/dev/ttyS0`. Run only one of them at a time against a single modem.

### 4. Uptime Monitor — `uptime/monitor_connection.py`

A simple loop that pings `TARGET_IP` every 5 seconds and plays
`network_connected.wav` / `network_disconnected.wav` on a fixed ALSA device when
the connection state changes (and repeats the disconnect chime while down).

---

## Hardware & system requirements

- Raspberry Pi (developed on Pi OS, Python 3.11).
- SIM800L GSM/GPRS module on `/dev/ttyS0` @ 115200 baud (for `call/` and `sms/`).
- One or more ALSA playback devices (USB sound cards, HDMI, headphone jack).
- System tools on `PATH`: `aplay`, `arecord`, `amixer` (`alsa-utils`), `ffmpeg`,
  `sox`, `ping`.

## Setup

### 1. Configuration (`.env`)

Secrets and host-specific settings are read from a `.env` file at the repo root
(loaded via `python-dotenv`). Copy the template and fill it in:

```bash
cp .env.example .env
# generate strong secrets:
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`.env` is **git-ignored** — never commit it. All three services read from it
(`FLASK_SECRET_KEY`, `DEFAULT_API_KEY`, serial ports, folder paths, web ports,
optional SIM PIN). Every value has a sensible default, so the apps still start if
`.env` is missing (a random session key is generated per run in that case).

### 2. Run the services

Each service has its own virtual environment in the original deployment. To run
from a clean checkout:

```bash
# Sound Server
cd flask-env
python3 -m venv .venv && source .venv/bin/activate
pip install -r ../requirements.txt          # Flask, gTTS, pydub, gunicorn, dotenv, ...
python app.py                                # dev server on :5000
# or: gunicorn -c gunicorn_config.py app:app

# SMS Gateway / Call Intercom (need pyserial + Flask + dotenv)
cd ../sms   # or ../call
python3 -m venv .venv && source .venv/bin/activate
pip install flask pyserial python-dotenv
python sms.py     # :5010   /   python call.py  (:5020)
```

The top-level `requirements.txt` captures the Sound Server's full environment
(note it also pins several `google-cloud-*` / `google-genai` packages from
earlier TTS experiments; only Flask, gTTS, pydub, python-dotenv, and gunicorn are
needed to run `app.py`).

### 3. Install as systemd services (Raspberry Pi)

On the Pi, the services run under **systemd**, fronted by a **Caddy portal on
port 80**. `deploy/install.sh` reconstructs the whole setup — it **uninstalls the
old unit(s)** then installs fresh ones for the current checkout (creating
virtualenvs, generating a `.env` with fresh secrets if none exists, and the Caddy
landing page):

```bash
sudo ./deploy/install.sh                 # (re)install soundserver.service only
sudo ./deploy/install.sh all             # every service + the Caddy portal
sudo ./deploy/install.sh call caddy      # pick specific components
sudo ./deploy/install.sh --uninstall all # only remove the services
```

It runs services as the user behind `sudo` (override with `SERVICE_USER=<name>`).
Manage them the usual way:

```bash
systemctl status soundserver.service
journalctl -u soundserver.service -f
```

| Component | Unit | Runs |
|-----------|------|------|
| **Portal** | `caddy` | Landing page on **HTTP :80** linking to the dashboards below |
| Sound Server | `soundserver.service` | `flask-env/app.py` via gunicorn, HTTP on :5000 |
| SMS Gateway | `sms_gateway.service` | `sms/sms.py` on :5010 |
| Call Intercom | `call_intercom.service` | `call/call.py` on :5020 |
| Uptime Monitor | `uptime_monitor.service` | `uptime/monitor_connection.py` (no dashboard) |

**The portal** (`deploy/caddy/`) is a static page served by Caddy at
`http://<pi>/`. It auto-discovers the Pi's hostname and links to each service's
own dashboard on its own port (with a best-effort online/offline indicator) — the
Flask apps are linked directly rather than reverse-proxied, since they use
absolute paths.

> **Note:** `sms_gateway` and `call_intercom` share the single SIM800L on
> `/dev/ttyS0` and cannot both run at once — `install.sh` warns about this and
> enables both, but stop one before starting the other.

### Preparing sounds

Source prompts live in `sound/*.mp3`. Convert them to the padded WAV files the
Sound Server plays with:

```bash
./mp3_to_padded_wav.sh   # ffmpeg adelay pad -> sound/wav/*.wav
```

Copy the resulting `.wav` files into the Sound Server's `SOUND_FOLDER`
(`/home/rs/flask-env/wav` in the deployed config).

---

## Configuration notes & paths

Several paths are **hard-coded** for the original Pi deployment and must be
adjusted for a new host:

- `flask-env/app.py`: `SOUND_FOLDER`, `TTS_CACHE_FOLDER` (`/home/rs/flask-env/...`).
- `uptime/monitor_connection.py`: `TARGET_IP`, WAV paths, `AUDIO_DEVICE`.
- `call/call.py` & `sms/sms.py`: `SERIAL_PORT` (`/dev/ttyS0`), `BAUD_RATE`.

## Security

⚠️ This project was written for a trusted LAN and is **not hardened for public
exposure**:

- Secrets (`FLASK_SECRET_KEY`, `DEFAULT_API_KEY`, SIM PIN, etc.) now load from a
  git-ignored `.env` file — see [Setup](#1-configuration-env). No secrets are
  hard-coded in the source. TLS `key.pem`/`cert.pem` are also git-ignored.
- Any secret that was committed in earlier history is **compromised and must be
  rotated** — generate fresh values into your `.env`.
- The web servers bind to `0.0.0.0` with no authentication on the play/SMS
  endpoints. Keep them behind a firewall / VPN and do not expose them to the
  internet.

## Repository layout

```
soundserver/
├── flask-env/            # Sound Server (Flask app + its venv)
│   ├── app.py            #   main service
│   ├── gunicorn_config.py
│   ├── templates/        #   index.html soundboard, login, api-key admin
│   ├── devices.json / categories.json / tags.json
│   └── wav/ tts_cache/   #   (git-ignored) generated audio
├── call/                 # GSM auto-answer intercom
│   ├── call.py
│   ├── call_config.json
│   └── templates/index.html
├── sms/                  # SMS gateway
│   └── sms.py
├── uptime/               # network up/down chime
│   └── monitor_connection.py
├── sound/                # master .mp3 prompt library
├── deploy/
│   ├── install.sh        # (un)install systemd services + Caddy portal
│   └── caddy/            # portal landing page + reference Caddyfile
├── mp3_to_padded_wav.sh  # mp3 -> padded wav converter
├── .env.example          # config template (copy to .env)
└── requirements.txt
```
