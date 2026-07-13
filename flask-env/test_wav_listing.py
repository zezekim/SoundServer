import os

SOUND_FOLDER = '/home/rs/flask-env/wav'

print(f"Checking folder: {SOUND_FOLDER}")

if os.path.exists(SOUND_FOLDER):
    print(f"'{SOUND_FOLDER}' exists.")
    if os.path.isdir(SOUND_FOLDER):
        print(f"'{SOUND_FOLDER}' is a directory.")
        try:
            print(f"Attempting to list contents of '{SOUND_FOLDER}'...")
            all_entries = os.listdir(SOUND_FOLDER)
            print(f"Raw entries found: {all_entries}")

            wav_files = [f for f in all_entries if f.lower().endswith('.wav')]
            print(f"Detected .wav files (case-insensitive): {wav_files}")

            if not wav_files and all_entries:
                print("WARNING: Folder has files, but none seem to end with .wav (case-insensitive).")
            elif not all_entries:
                print("WARNING: Folder exists but is empty.")

        except PermissionError:
            print(f"ERROR: Permission denied when trying to read '{SOUND_FOLDER}'.")
        except Exception as e:
            print(f"ERROR: An unexpected error occurred: {e}")
    else:
        print(f"ERROR: '{SOUND_FOLDER}' is not a directory.")
else:
    print(f"ERROR: '{SOUND_FOLDER}' does not exist.")