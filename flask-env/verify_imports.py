print("--- Starting Verification Script ---")

try:
    print("Checking for Flask...")
    import flask
    print("Flask OK.")

    print("\nChecking for gTTS...")
    import gtts
    print("gTTS OK.")

    print("\nChecking for pydub...")
    import pydub
    print("pydub OK.")

    print("\nAttempting to import the main app...")
    import app
    print("\nSUCCESS: All libraries are present and app.py was imported successfully!")

except ImportError as e:
    print(f"\n--- !!! IMPORT ERROR !!! ---")
    print(f"A required library is missing.")
    print(f"Error message: {e}")
    print(f"Please activate your virtual environment ('source bin/activate') and run 'pip install gTTS pydub'")

except Exception as e:
    print(f"\n--- !!! UNEXPECTED ERROR !!! ---")
    print(f"An error occurred while importing app.py:")
    print(e)
    import traceback
    traceback.print_exc()