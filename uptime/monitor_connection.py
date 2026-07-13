#!/usr/bin/env python3

import subprocess
import time
import os

# --- Configuration ---
TARGET_IP = "10.0.14.2"
CONNECTED_WAV = "/home/rs/uptime/wav/network_connected.wav"
DISCONNECTED_WAV = "/home/rs/uptime/wav/network_disconnected.wav"
AUDIO_DEVICE = "hw:2,0"  # Corresponds to card 2, device 0
PING_TIMEOUT = 1  # Seconds to wait for a ping response
CHECK_INTERVAL = 5 # Seconds between checks / repeat interval for disconnected sound
APLAY_PATH = "/usr/bin/aplay" # Full path to aplay
PING_PATH = "/bin/ping"       # Full path to ping (verify with 'which ping')
# ---------------------

def check_file_exists(filepath):
    """Checks if a file exists."""
    if not os.path.exists(filepath):
        print(f"Error: Audio file not found at {filepath}")
        return False
    return True

def ping_host(ip_address, timeout):
    """
    Pings the specified IP address.
    Returns True if reachable, False otherwise.
    """
    try:
        result = subprocess.run(
            [PING_PATH, "-c", "1", f"-W{timeout}", ip_address],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False
        )
        return result.returncode == 0
    except FileNotFoundError:
        print(f"Error: '{PING_PATH}' command not found. Please verify the path.")
        return False
    except Exception as e:
        print(f"An error occurred during ping: {e}")
        return False

def play_sound(wav_file, device):
    """
    Plays the specified WAV file using aplay on the specified device.
    """
    if not check_file_exists(wav_file):
        return

    try:
        print(f"Playing: {os.path.basename(wav_file)}")
        subprocess.run(
            [APLAY_PATH, "-D", device, wav_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
    except FileNotFoundError:
        print(f"Error: '{APLAY_PATH}' command not found. Is 'alsa-utils' installed and path correct?")
    except subprocess.CalledProcessError as e:
        print(f"Error playing sound: {e}. Check audio device '{device}' and file '{wav_file}'.")
    except Exception as e:
        print(f"An unexpected error occurred during sound playback: {e}")


def main():
    """
    Main monitoring loop.
    """
    print("Starting network connection monitor (with disconnect repeat)...")
    print(f"Target IP: {TARGET_IP}")
    print(f"Audio Device: {AUDIO_DEVICE}")

    # Check for critical files before starting
    if not os.path.exists(APLAY_PATH):
        print(f"CRITICAL ERROR: aplay not found at {APLAY_PATH}. Exiting.")
        return
    if not os.path.exists(PING_PATH):
        print(f"CRITICAL ERROR: ping not found at {PING_PATH}. Exiting.")
        return
    if not check_file_exists(CONNECTED_WAV) or not check_file_exists(DISCONNECTED_WAV):
        print("Exiting due to missing audio files.")
        return

    previous_status = None

    while True:
        current_status = ping_host(TARGET_IP, PING_TIMEOUT)

        if current_status:
            # We are connected
            if previous_status is False or previous_status is None:
                # We JUST reconnected or started up while connected
                print(f"Connection to {TARGET_IP} established.")
                play_sound(CONNECTED_WAV, AUDIO_DEVICE)
            # Update status only if it changed
            previous_status = True
        else:
            # We are disconnected
            if previous_status is True:
                 print(f"Connection to {TARGET_IP} lost. Starting disconnect sound loop...")
            else:
                 print(f"Connection still lost. Playing disconnect sound again...")
            # Play disconnect sound EVERY time we check and find it disconnected
            play_sound(DISCONNECTED_WAV, AUDIO_DEVICE)
            previous_status = False

        # Wait 5 seconds before the next check.
        # This also acts as the 5-second interval for the disconnect sound.
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()