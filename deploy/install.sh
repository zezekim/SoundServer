#!/usr/bin/env bash
#
# SoundServer service installer / re-installer.
#
# Uninstalls the OLD systemd unit(s) (stop, disable, remove) and installs the
# NEW ones for this checkout — creating per-component virtualenvs, ensuring a
# .env, generating a self-signed TLS cert for the Sound Server, and wiring the
# gunicorn-based service exactly as documented in history.txt.
#
# Reconstructed from history.txt. The services that were installed as systemd
# units on the original Raspberry Pi:
#     soundserver.service    -> flask-env/app.py via gunicorn (HTTPS :5000)
#     sms_gateway.service     -> sms/sms.py           (:5010)
#     uptime_monitor.service  -> uptime/monitor_connection.py
# (call/call.py was always run manually, so it has no service.)
#
# Usage:
#     sudo ./deploy/install.sh                 # (re)install soundserver only
#     sudo ./deploy/install.sh all             # soundserver + sms + uptime
#     sudo ./deploy/install.sh soundserver sms # pick specific components
#     sudo ./deploy/install.sh --uninstall all # only remove, don't reinstall
#
# Env overrides:
#     SERVICE_USER=pi sudo -E ./deploy/install.sh    # run services as this user
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Paths & settings
# ---------------------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLASK_DIR="$REPO_DIR/flask-env"
SMS_DIR="$REPO_DIR/sms"
UPTIME_DIR="$REPO_DIR/uptime"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

# The user the services run as: explicit env > the human behind sudo > repo owner > rs.
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(stat -c '%U' "$REPO_DIR" 2>/dev/null || echo rs)}}"

C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'; C_RED=$'\033[0;31m'; C_BLUE=$'\033[0;34m'; C_OFF=$'\033[0m'
log()  { printf '%s==>%s %s\n'  "$C_BLUE"   "$C_OFF" "$*"; }
ok()   { printf '%s  ✓%s %s\n'  "$C_GREEN"  "$C_OFF" "$*"; }
warn() { printf '%s  !%s %s\n'  "$C_YELLOW" "$C_OFF" "$*"; }
die()  { printf '%s✗ %s%s\n'    "$C_RED"    "$*" "$C_OFF" >&2; exit 1; }

UNINSTALL_ONLY=0
COMPONENTS=()

usage() {
    sed -n '3,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        -h|--help)       usage 0 ;;
        --uninstall)     UNINSTALL_ONLY=1 ;;
        all)             COMPONENTS=(soundserver sms uptime) ;;
        soundserver|sound) COMPONENTS+=(soundserver) ;;
        sms)             COMPONENTS+=(sms) ;;
        uptime)          COMPONENTS+=(uptime) ;;
        call)            warn "call.py has no systemd service (it was run manually) — skipping." ;;
        *)               die "Unknown argument: '$arg' (try --help)" ;;
    esac
done
# Default target: the flagship Sound Server.
[ ${#COMPONENTS[@]} -eq 0 ] && COMPONENTS=(soundserver)

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "Please run with sudo (needs to manage systemd)."
command -v systemctl >/dev/null 2>&1 || die "systemctl not found — this installer targets systemd hosts."
id "$SERVICE_USER" >/dev/null 2>&1 || die "Service user '$SERVICE_USER' does not exist. Set SERVICE_USER=<name>."

log "Repo:          $REPO_DIR"
log "Service user:  $SERVICE_USER"
log "Components:    ${COMPONENTS[*]}"
log "Mode:          $([ $UNINSTALL_ONLY -eq 1 ] && echo 'uninstall only' || echo 'uninstall old + install new')"
echo

# Map a component name to its unit-file name.
unit_name() {
    case "$1" in
        soundserver) echo "soundserver.service" ;;
        sms)         echo "sms_gateway.service" ;;
        uptime)      echo "uptime_monitor.service" ;;
    esac
}

# ---------------------------------------------------------------------------
# Uninstall an existing unit
# ---------------------------------------------------------------------------
uninstall_unit() {
    local unit="$1"
    if systemctl list-unit-files "$unit" >/dev/null 2>&1 && systemctl cat "$unit" >/dev/null 2>&1; then
        log "Removing old $unit"
        systemctl stop "$unit"    2>/dev/null || true
        systemctl disable "$unit" 2>/dev/null || true
        rm -f "$SYSTEMD_DIR/$unit"
        ok "$unit removed"
    else
        warn "$unit not currently installed — nothing to remove"
        rm -f "$SYSTEMD_DIR/$unit" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# System package + venv helpers
# ---------------------------------------------------------------------------
# ensure_system_pkgs <cmd...> — checks each command; apt-installs the package that
# provides it (command name != package name for some, e.g. aplay -> alsa-utils).
ensure_system_pkgs() {
    local cmd pkg missing=()
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 && continue
        case "$cmd" in
            aplay|arecord|amixer) pkg="alsa-utils" ;;
            ping)                 pkg="iputils-ping" ;;
            *)                    pkg="$cmd" ;;
        esac
        missing+=("$pkg")
    done
    [ ${#missing[@]} -eq 0 ] && return 0
    # de-duplicate package list
    IFS=$'\n' read -r -d '' -a missing < <(printf '%s\n' "${missing[@]}" | sort -u && printf '\0')
    if command -v apt-get >/dev/null 2>&1; then
        log "Installing system packages: ${missing[*]}"
        apt-get update -qq && apt-get install -y "${missing[@]}" >/dev/null
        ok "system packages installed"
    else
        warn "Missing packages (${missing[*]}) and apt-get unavailable — install them manually."
    fi
}

# ensure_venv <dir> <pkg...>  -> echoes path to the venv's bin/ directory
ensure_venv() {
    local dir="$1"; shift
    local bindir
    if [ -x "$dir/bin/python3" ] && [ -f "$dir/pyvenv.cfg" ]; then
        bindir="$dir/bin"                       # legacy `python -m venv .` inside the component dir
    elif [ -x "$dir/venv/bin/python3" ]; then
        bindir="$dir/venv/bin"                  # legacy `python -m venv venv` subdir
    elif [ -x "$dir/.venv/bin/python3" ]; then
        bindir="$dir/.venv/bin"
    else
        log "Creating virtualenv in $dir/.venv" >&2
        sudo -u "$SERVICE_USER" python3 -m venv "$dir/.venv"
        bindir="$dir/.venv/bin"
    fi
    log "Installing Python deps ($*) into ${bindir%/bin}" >&2
    sudo -u "$SERVICE_USER" "$bindir/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    sudo -u "$SERVICE_USER" "$bindir/pip" install --quiet "$@" >&2
    echo "$bindir"
}

ensure_env_file() {
    local env="$REPO_DIR/.env"
    if [ -f "$env" ]; then
        ok ".env already present — leaving it untouched"
        return
    fi
    log "Creating .env with freshly generated secrets"
    local sk ak ck
    sk="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
    ak="$(python3 -c 'import secrets;print(secrets.token_urlsafe(36))')"
    ck="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
    cat > "$env" <<ENV
# Local secrets — git-ignored. Generated by deploy/install.sh. Do NOT commit.

# ── Sound Server (flask-env/app.py) ──
FLASK_SECRET_KEY=$sk
DEFAULT_API_KEY=$ak
SOUND_FOLDER=$FLASK_DIR/wav
TTS_CACHE_FOLDER=$FLASK_DIR/tts_cache
SOUND_SERVER_PORT=5000

# ── Call Intercom (call/call.py) ──
CALL_FLASK_SECRET_KEY=$ck
CALL_SERIAL_PORT=/dev/ttyS0
CALL_BAUD_RATE=115200
CALL_WEB_PORT=5020

# ── SMS Gateway (sms/sms.py) ──
SMS_SERIAL_PORT=/dev/ttyS0
SMS_BAUD_RATE=115200
SMS_WEB_PORT=5010
SMS_PIN_CODE=
SMS_PHONE_NUMBER=
ENV
    chown "$SERVICE_USER" "$env"; chmod 600 "$env"
    ok ".env created (secrets generated, paths point at $FLASK_DIR)"
}

write_unit() {  # write_unit <unit-name> <content>
    local unit="$1" content="$2"
    printf '%s\n' "$content" > "$SYSTEMD_DIR/$unit"
    chmod 644 "$SYSTEMD_DIR/$unit"
    ok "wrote $SYSTEMD_DIR/$unit"
}

enable_start() {
    local unit="$1"
    systemctl daemon-reload
    systemctl enable "$unit" >/dev/null 2>&1
    systemctl restart "$unit"
    sleep 1
    if systemctl is-active --quiet "$unit"; then
        ok "$unit is running"
    else
        warn "$unit did not become active — check: journalctl -u $unit -n 40 --no-pager"
    fi
}

# ---------------------------------------------------------------------------
# Per-component install routines
# ---------------------------------------------------------------------------
install_soundserver() {
    local port bindir
    port="$(grep -E '^SOUND_SERVER_PORT=' "$REPO_DIR/.env" 2>/dev/null | cut -d= -f2)"; port="${port:-5000}"
    ensure_system_pkgs ffmpeg sox aplay openssl
    mkdir -p "$FLASK_DIR/wav" "$FLASK_DIR/tts_cache"
    chown -R "$SERVICE_USER" "$FLASK_DIR/wav" "$FLASK_DIR/tts_cache"

    # Self-signed TLS cert for gunicorn (matches history: openssl req -x509 ...).
    if [ ! -f "$FLASK_DIR/cert.pem" ] || [ ! -f "$FLASK_DIR/key.pem" ]; then
        log "Generating self-signed TLS certificate"
        openssl req -x509 -newkey rsa:4096 -nodes -days 365 \
            -keyout "$FLASK_DIR/key.pem" -out "$FLASK_DIR/cert.pem" \
            -subj "/CN=soundserver.local" >/dev/null 2>&1
        chown "$SERVICE_USER" "$FLASK_DIR/key.pem" "$FLASK_DIR/cert.pem"
        chmod 600 "$FLASK_DIR/key.pem"
        ok "TLS cert generated"
    fi

    bindir="$(ensure_venv "$FLASK_DIR" Flask gunicorn gTTS pydub python-dotenv)"
    usermod -aG audio "$SERVICE_USER" 2>/dev/null || true

    write_unit "soundserver.service" "[Unit]
Description=SoundServer — Flask audio control panel (gunicorn)
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
SupplementaryGroups=audio
WorkingDirectory=$FLASK_DIR
ExecStart=$bindir/gunicorn --workers 1 -c $FLASK_DIR/gunicorn_config.py --bind 0.0.0.0:$port --certfile=$FLASK_DIR/cert.pem --keyfile=$FLASK_DIR/key.pem app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target"
    enable_start "soundserver.service"
    ok "Sound Server: https://<pi-ip>:$port"
}

install_sms() {
    local port bindir
    port="$(grep -E '^SMS_WEB_PORT=' "$REPO_DIR/.env" 2>/dev/null | cut -d= -f2)"; port="${port:-5010}"
    bindir="$(ensure_venv "$SMS_DIR" Flask pyserial python-dotenv)"
    usermod -aG dialout "$SERVICE_USER" 2>/dev/null || true

    write_unit "sms_gateway.service" "[Unit]
Description=SoundServer SMS Gateway (SIM800L)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
SupplementaryGroups=dialout
WorkingDirectory=$SMS_DIR
ExecStart=$bindir/python $SMS_DIR/sms.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target"
    enable_start "sms_gateway.service"
    ok "SMS Gateway: http://<pi-ip>:$port"
}

install_uptime() {
    ensure_system_pkgs aplay ping
    usermod -aG audio "$SERVICE_USER" 2>/dev/null || true
    write_unit "uptime_monitor.service" "[Unit]
Description=SoundServer Network Uptime Monitor
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
SupplementaryGroups=audio
WorkingDirectory=$UPTIME_DIR
ExecStart=/usr/bin/python3 $UPTIME_DIR/monitor_connection.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target"
    enable_start "uptime_monitor.service"
    warn "uptime: edit TARGET_IP / AUDIO_DEVICE / WAV paths in uptime/monitor_connection.py if needed."
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
# 1) Always uninstall the old units first.
for comp in "${COMPONENTS[@]}"; do
    uninstall_unit "$(unit_name "$comp")"
done
systemctl daemon-reload
echo

if [ "$UNINSTALL_ONLY" -eq 1 ]; then
    ok "Uninstall complete."
    exit 0
fi

# 2) Ensure shared config, then install each selected component.
ensure_env_file
echo
for comp in "${COMPONENTS[@]}"; do
    log "Installing $comp …"
    case "$comp" in
        soundserver) install_soundserver ;;
        sms)         install_sms ;;
        uptime)      install_uptime ;;
    esac
    echo
done

ok "Done. Manage with:  systemctl status <service>   ·   journalctl -u <service> -f"
