"""Gunicorn entrypoint."""
import os

from app import app

if __name__ == "__main__":
    # Local debugging only; production runs gunicorn directly via systemd.
    port = int(os.environ.get("FLASK_PORT", "19998"))
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)