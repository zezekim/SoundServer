def post_fork(server, worker):
    # This hook will now try to import the app and log any success or failure.
    try:
        server.log.info("GUNICORN HOOK: Attempting to import 'app' module...")
        import app
        server.log.info("GUNICORN HOOK: SUCCESS! 'app' module was imported.")

        server.log.info("GUNICORN HOOK: Attempting to start audio_worker thread...")
        import threading
        worker_thread = threading.Thread(target=app.audio_worker, daemon=True)
        worker_thread.start()
        server.log.info("GUNICORN HOOK: SUCCESS! Audio worker thread was started.")

    except Exception as e:
        server.log.error("!!! GUNICORN HOOK FAILED !!!")
        server.log.error(f"The error is: {e}")
        import traceback
        server.log.error(traceback.format_exc())