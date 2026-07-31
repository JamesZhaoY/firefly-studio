"""Gunicorn entrypoint for Render / Heroku / Fly / Docker."""
import os

from app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)