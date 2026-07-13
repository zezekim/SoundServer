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

On the Pi they run as **systemd services** (`soundserver.service`,
`sms_gateway.service`, `uptime_monitor.service`). `deploy/install.sh` uninstalls
the old units and installs fresh ones for the current checkout — if you change a
service's entry point, ports, or venv layout, update the matching unit heredoc in
that script. `call.py` has no unit (run manually).

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
- Secrets load from a git-ignored **`.env`** at the repo root via
  `python-dotenv` (`load_dotenv()` is called near the top of each app; keys are
  read with `os.environ.get(...)` and safe defaults). Add new config the same
  way — put the key in `.env.example` with a placeholder, read it with a default,
  and **never hard-code a secret**. `key.pem`/`cert.pem` and `api_keys.*` are
  also git-ignored. Treat any previously committed value as compromised.

## Host-specific config

Most host-specific values are now **`.env`-overridable** with the old hard-coded
values as defaults:

- `flask-env/app.py`: `SOUND_FOLDER`, `TTS_CACHE_FOLDER`, `SOUND_SERVER_PORT`.
- `call.py` / `sms.py`: `*_SERIAL_PORT` (`/dev/ttyS0`), `*_BAUD_RATE`, `*_WEB_PORT`,
  `SMS_PIN_CODE`, `SMS_PHONE_NUMBER`.

Still hard-coded (adjust in source if porting): `uptime/monitor_connection.py`
(`TARGET_IP`, WAV paths, `AUDIO_DEVICE = hw:2,0`) and the absolute tool paths
(`/usr/bin/aplay`, etc.) in every service.

## Dashboard (`flask-env/templates/index.html`)

Single-file template + vanilla JS (SortableJS from CDN). It consumes the Jinja
vars `files`, `discovered_devices` (`hw_id`/`ha_name`/`card_index`),
`cards_for_labeling`, and `SOUND_FOLDER`, and drives the JSON/`/play` API
verbatim — **preserve those endpoint shapes when editing routes.** UX model: a
global *active speaker* + *repeat* + *background* chosen in the sticky header;
tapping a sound plays it there. A separate **Edit-layout mode** toggles SortableJS
drag/drop and category rename/delete; "Save" posts to `/api/save_layout`.
Tabbed sidebar = Speak (`/api/speak`), Volume (`/api/card/.../mixer_controls`,
`.../current_volume`, `/api/set_volume`), Upload (`/upload`), Speakers
(`/update_label`). Theme is light/dark via `data-theme` on `<html>`.

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
