#!/usr/bin/env bash
#
# SoundServer service installer / re-installer.
#
# Uninstalls the OLD systemd unit(s) (stop, disable, remove) and installs the
# NEW ones for this checkout — creating per-component virtualenvs, ensuring a
# .env, and wiring the gunicorn-based Sound Server plus the SMS, Call, uptime,
# and Caddy-portal services.
#
# Components:
#     soundserver.service     -> flask-env/app.py via gunicorn (HTTPS :5000)
#     sms_gateway.service     -> sms/sms.py            (:5010)
#     call_intercom.service   -> call/call.py          (:5020)
#     uptime_monitor.service  -> uptime/monitor_connection.py
#     caddy (portal)          -> Caddy landing page on HTTP :80 linking to the
#                                dashboards above (soundserver, sms, call)
#
# Usage:
#     sudo ./deploy/install.sh                 # (re)install soundserver only
#     sudo ./deploy/install.sh all             # every service + the Caddy portal
#     sudo ./deploy/install.sh call caddy      # pick specific components
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
CALL_DIR="$REPO_DIR/call"
UPTIME_DIR="$REPO_DIR/uptime"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
PORTAL_ROOT="${PORTAL_ROOT:-/var/www/soundserver}"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"

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
    # Print the header comment block (from line 3 to the first non-comment line).
    awk 'NR<3{next} /^[^#]/{exit} {sub(/^# ?/,""); print}' "${BASH_SOURCE[0]}"
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        -h|--help)       usage 0 ;;
        --uninstall)     UNINSTALL_ONLY=1 ;;
        all)             COMPONENTS=(soundserver sms call uptime caddy) ;;
        soundserver|sound) COMPONENTS+=(soundserver) ;;
        sms)             COMPONENTS+=(sms) ;;
        call)            COMPONENTS+=(call) ;;
        uptime)          COMPONENTS+=(uptime) ;;
        caddy|portal)    COMPONENTS+=(caddy) ;;
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

# The SMS gateway and Call intercom share the single SIM800L on /dev/ttyS0 and
# cannot both be active at once.
if [ "$UNINSTALL_ONLY" -eq 0 ] && printf '%s\n' "${COMPONENTS[@]}" | grep -qx sms \
   && printf '%s\n' "${COMPONENTS[@]}" | grep -qx call; then
    warn "sms_gateway and call_intercom both use /dev/ttyS0 — only one can run at a time."
    warn "Both will be enabled; stop one before starting the other (systemctl stop <svc>)."
fi
echo

# Map a component name to its unit-file name ('' for non-systemd components).
unit_name() {
    case "$1" in
        soundserver) echo "soundserver.service" ;;
        sms)         echo "sms_gateway.service" ;;
        call)        echo "call_intercom.service" ;;
        uptime)      echo "uptime_monitor.service" ;;
        *)           echo "" ;;
    esac
}

# Read a KEY=value from the repo .env, with a fallback default.
getenv() {
    local val
    val="$(grep -E "^$1=" "$REPO_DIR/.env" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d '[:space:]')"
    echo "${val:-$2}"
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
    port="$(getenv SOUND_SERVER_PORT 5000)"
    ensure_system_pkgs ffmpeg sox aplay
    mkdir -p "$FLASK_DIR/wav" "$FLASK_DIR/tts_cache"
    chown -R "$SERVICE_USER" "$FLASK_DIR/wav" "$FLASK_DIR/tts_cache"

    bindir="$(ensure_venv "$FLASK_DIR" Flask gunicorn gTTS pydub python-dotenv)"
    usermod -aG audio "$SERVICE_USER" 2>/dev/null || true

    # Plain HTTP on the LAN — automations call it over http and the port-80 portal
    # links to it directly. (Put TLS in front with Caddy if you ever need it.)
    write_unit "soundserver.service" "[Unit]
Description=SoundServer — Flask audio control panel (gunicorn)
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
SupplementaryGroups=audio
WorkingDirectory=$FLASK_DIR
ExecStart=$bindir/gunicorn --workers 1 -c $FLASK_DIR/gunicorn_config.py --bind 0.0.0.0:$port app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target"
    enable_start "soundserver.service"
    ok "Sound Server: http://<pi-ip>:$port"
}

install_sms() {
    local port bindir
    port="$(getenv SMS_WEB_PORT 5010)"
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

install_call() {
    local port bindir
    port="$(getenv CALL_WEB_PORT 5020)"
    # call.py drives the modem (serial) AND plays/records audio (arecord|sox|aplay).
    ensure_system_pkgs sox aplay arecord
    bindir="$(ensure_venv "$CALL_DIR" Flask pyserial python-dotenv)"
    usermod -aG dialout,audio "$SERVICE_USER" 2>/dev/null || true

    write_unit "call_intercom.service" "[Unit]
Description=SoundServer Call Intercom (SIM800L auto-answer + broadcast)
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
SupplementaryGroups=dialout audio
WorkingDirectory=$CALL_DIR
ExecStart=$bindir/python $CALL_DIR/call.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target"
    enable_start "call_intercom.service"
    ok "Call Intercom: http://<pi-ip>:$port"
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

# --- Caddy portal (HTTP :80 landing page linking to the dashboards) ---
ensure_caddy() {
    command -v caddy >/dev/null 2>&1 && return 0
    if ! command -v apt-get >/dev/null 2>&1; then
        die "Caddy is not installed and apt-get is unavailable. Install Caddy, then re-run with 'caddy'."
    fi
    log "Installing Caddy from its official apt repository"
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq && apt-get install -y caddy >/dev/null
    command -v caddy >/dev/null 2>&1 || die "Caddy auto-install failed — install it manually and re-run."
    ok "Caddy installed"
}

install_caddy() {
    ensure_caddy
    local src="$REPO_DIR/deploy/caddy/site"
    [ -f "$src/index.html" ] || die "Portal page not found at $src/index.html"

    # Publish the portal to a world-readable path (the caddy user can't traverse /home).
    log "Publishing portal to $PORTAL_ROOT"
    mkdir -p "$PORTAL_ROOT"
    install -m 0644 "$src/index.html" "$PORTAL_ROOT/index.html"
    # Inject the live ports so the landing page links to the right places.
    cat > "$PORTAL_ROOT/config.js" <<CFG
window.SS_PORTS = { sound: $(getenv SOUND_SERVER_PORT 5000), sms: $(getenv SMS_WEB_PORT 5010), call: $(getenv CALL_WEB_PORT 5020) };
CFG
    chmod -R a+rX "$PORTAL_ROOT"

    # Write the Caddyfile (backing up any pre-existing one, once).
    mkdir -p "$(dirname "$CADDYFILE")"
    if [ -f "$CADDYFILE" ] && ! grep -q 'Managed by SoundServer' "$CADDYFILE" 2>/dev/null; then
        cp "$CADDYFILE" "$CADDYFILE.pre-soundserver"
        warn "Backed up existing Caddyfile to $CADDYFILE.pre-soundserver"
    fi
    cat > "$CADDYFILE" <<CADDY
# Managed by SoundServer deploy/install.sh — edit deploy/caddy/ and re-run.
:80 {
    root * $PORTAL_ROOT
    file_server
    encode gzip
}
CADDY
    systemctl enable caddy >/dev/null 2>&1 || true
    if command -v caddy >/dev/null 2>&1 && caddy validate --config "$CADDYFILE" >/dev/null 2>&1; then
        systemctl reload caddy 2>/dev/null || systemctl restart caddy
    else
        systemctl restart caddy
    fi
    sleep 1
    if systemctl is-active --quiet caddy; then
        ok "Portal is live:  http://<pi-ip>/  (port 80)"
    else
        warn "caddy did not become active — check: journalctl -u caddy -n 40 --no-pager"
    fi
}

uninstall_caddy() {
    if [ ! -f "$CADDYFILE" ] || ! grep -q 'Managed by SoundServer' "$CADDYFILE" 2>/dev/null; then
        warn "Caddy portal not managed here — leaving Caddy untouched."
    elif [ -f "$CADDYFILE.pre-soundserver" ]; then
        mv "$CADDYFILE.pre-soundserver" "$CADDYFILE"
        systemctl reload caddy 2>/dev/null || systemctl restart caddy 2>/dev/null || true
        ok "Restored previous Caddyfile"
    else
        rm -f "$CADDYFILE"
        systemctl stop caddy 2>/dev/null || true
        systemctl disable caddy 2>/dev/null || true
        ok "Removed SoundServer Caddyfile and stopped caddy"
    fi
    rm -rf "$PORTAL_ROOT"
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
# 1) Always uninstall the old services first.
for comp in "${COMPONENTS[@]}"; do
    if [ "$comp" = "caddy" ]; then
        uninstall_caddy
    else
        uninstall_unit "$(unit_name "$comp")"
    fi
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
        call)        install_call ;;
        uptime)      install_uptime ;;
        caddy)       install_caddy ;;
    esac
    echo
done

ok "Done. Manage with:  systemctl status <service>   ·   journalctl -u <service> -f"
