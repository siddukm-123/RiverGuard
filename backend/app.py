import logging
import os
from pathlib import Path

from flask import Flask, send_from_directory, request

import config
from api.routes import api_bp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")
app.secret_key = config.FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024

app.register_blueprint(api_bp)


@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        return "", 204


@app.after_request
def add_cors_headers(response):
    """Allow Vercel frontend to call Railway API directly if not using rewrites."""
    origin = config.CORS_ORIGIN
    if origin == "*":
        response.headers["Access-Control-Allow-Origin"] = "*"
    elif origin:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/")
def dashboard():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/detect")
def detect_page():
    return send_from_directory(FRONTEND_DIR, "upload.html")


@app.route("/details")
def details_page():
    return send_from_directory(FRONTEND_DIR, "details.html")


def _bootstrap_scheduler():
    if os.getenv("ENABLE_SCHEDULER", "true").lower() != "true":
        return
    try:
        from scheduler import start_scheduler
        start_scheduler(check_interval_minutes=60)
        logging.getLogger(__name__).info("Background scan scheduler started.")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Scheduler not started: {e}")


_bootstrap_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.FLASK_PORT, debug=config.FLASK_DEBUG, threaded=True)
