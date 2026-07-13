# CLAUDE.md

Guidance for Claude Code (and other AI assistants) working in this repository.

## What this is

SoundServer is **four independent Raspberry Pi services** sharing one repo, not a
single application. Each directory is its own runnable program with its own
config and (in the original deployment) its own virtualenv. Read the top-level
`README.md` for the full feature list; this file covers how to work in the code.

| Service | Entry point | Port | Talks to |
|---------|-------------|------|----------|
| Sound Server | `flask-env/app.py` | 5000 | ALSA (`aplay`/`amixer`), `ffmpeg`, `gTTS`, `pydub` |
| Call Intercom | `call/call.py` | 5020 | SIM800L on `/dev/ttyS0`, `arecord\|sox\|aplay` |
| SMS Gateway | `sms/sms.py` | 5010 | SIM800L on `/dev/ttyS0` |
| Uptime Monitor | `uptime/monitor_connection.py` | — | `ping`, `aplay` |

There is **no build step and no test suite.** These are plain Python scripts run
directly (or the Sound Server under gunicorn via `flask-env/gunicorn_config.py`).

## Architecture patterns to preserve

- **Serialized playback via a worker thread.** `flask-env/app.py` never calls
  `aplay` from a request handler. Requests enqueue a job onto `playback_queue`;
  the single `audio_worker` thread plays them one at a time and handles mixing
  and temp-file cleanup. Keep new playback paths going through the queue.
- **Serial access is lock-protected.** `sms.py` guards the modem with
  `serial_lock`; `call.py` uses its own `send_at_command` loop. The SIM800L is a
  single shared resource — never issue AT commands without holding the lock, and
  remember **`call.py` and `sms.py` cannot run simultaneously** against one modem.
- **Config is JSON on disk, loaded under a lock.** `call.py` uses `config_lock`
  (an `RLock`) around `config` reads/writes and always `save_config()` after
  mutating. `app.py` reads/writes `devices.json` / `categories.json` /
  `tags.json` directly. Match the existing load-defaults-then-merge pattern.
- **AT-command helpers** (`send_at_command`) send a command, then read lines
  until a success/error marker or timeout. Reuse them rather than writing raw
  `ser.write`/`readline` loops.

## House style

- Single-file Flask apps; routes and helpers live together in the entry-point
  file. HTML is either a Jinja template (`templates/index.html`) or an inline
  `render_template_string` (see `sms.py`). Match whichever the file already uses.
- Terse, one-line-heavy Python with inline `print(...)`/`logging` for
  diagnostics. Keep additions consistent with the surrounding density rather than
  reformatting existing code.
- All shelling out uses **absolute tool paths** (`/usr/bin/aplay`,
  `/usr/bin/amixer`, `/usr/bin/ffmpeg`, `/bin/ping`). Preserve this.

## Security-sensitive spots (be careful editing)

- Filenames/paths from HTTP are user input. Existing routes guard against `..`,
  leading `/`, and enforce the sound folder as a prefix; `secure_filename` is
  used on uploads/renames. **Keep these checks** on any new file-handling route.
- `device_id` flows into `aplay -D hw:<device_id>` — `app.py` rejects `..` in it.
- Secrets (`secret_key`, `DEFAULT_API_KEY`, `key.pem`/`cert.pem`, `api_keys.*`)
  are **git-ignored** and should never be re-committed. Don't hard-code new
  secrets; read them from env or an un-tracked file. Treat any previously
  committed value as compromised.

## Hard-coded paths (adjust per host, don't assume portability)

- `flask-env/app.py`: `SOUND_FOLDER`, `TTS_CACHE_FOLDER` → `/home/rs/flask-env/...`
- `uptime/monitor_connection.py`: `TARGET_IP`, WAV paths, `AUDIO_DEVICE = hw:2,0`
- `call.py` / `sms.py`: `SERIAL_PORT = /dev/ttyS0`, `BAUD_RATE = 115200`

## Running / verifying

Most functionality needs real Pi hardware (ALSA cards + SIM800L) and **cannot be
exercised on a dev laptop.** When changing a service:

- Sound Server: `cd flask-env && python app.py` (or `gunicorn -c gunicorn_config.py app:app`).
  Sanity-check routes with `curl` against `:5000` (`/api/speakers`, `/api/sounds`).
- SMS/Call: require the modem; `call/serial_test.py` is a standalone script for
  poking the SIM800L (`AT`, `AT+CPIN?`, `AT+CSQ`).
- There are no automated tests — verify by exercising the affected endpoint or
  loop and reading the log output.

## Things NOT to commit (already in `.gitignore`)

Virtualenvs (`flask-env/{bin,lib,...}`, `sms/venv`, `call/venv-call`),
`*.pem`, `api_keys.*`, `*.tar`, generated audio (`flask-env/wav`, `tts_cache`,
`uploads`, `sound/wav`), and runtime logs/CSVs. Source `.mp3` prompts in
`sound/` **are** tracked.
