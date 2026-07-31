"""Gunicorn entrypoint for Docker / Render / Fly."""
import os

from app import app

if __name__ == "__main__":
    # In containers we run gunicorn directly via the Dockerfile CMD, not this
    # file, but having it lets you `python wsgi.py` for local debugging.
    port = int(os.environ.get("PORT", "19999"))
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)