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

_log = logging.getLogger(__name__)
if config.FLASK_SECRET_KEY == "dev-secret-change-me" and not config.FLASK_DEBUG:
    _log.warning("FLASK_SECRET_KEY is the insecure default - set a long random value on Railway.")
if not config.RIVERGUARD_API_KEY:
    _log.warning("RIVERGUARD_API_KEY is not set: anyone who knows the URL can upload, scan, add or delete rivers. "
                 "Set it on Railway to require X-API-Key on write requests.")


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
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.errorhandler(413)
def _too_large(e):
    return {"error": f"File too large. Maximum upload is {config.MAX_UPLOAD_MB} MB.", "code": "payload_too_large"}, 413


@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith("/api/"):
        return {"error": "Not found", "code": "not_found"}, 404
    return e


@app.route("/")
def dashboard():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/detect")
def detect_page():
    return send_from_directory(FRONTEND_DIR, "upload.html")


@app.route("/details")
def details_page():
    return send_from_directory(FRONTEND_DIR, "details.html")


_scheduler_started = False


def bootstrap_scheduler():
    """
    Starts the APScheduler background thread (idempotent — safe to call more than once).

    IMPORTANT: this must NOT run at module import time when running under
    `gunicorn --preload`. Preload imports this module once in the master
    process, then forks worker processes. Starting a background thread
    before that fork is a classic gunicorn gotcha — the forked worker can
    inherit a half-locked thread state and hang forever, which looks
    exactly like a healthcheck timeout with no useful error in the logs.
    That's why this is called lazily on the first request below, instead
    of at the top of this file: only a forked worker ever handles a
    request, so this is guaranteed to run after the fork, never before it.
    """
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    if os.getenv("ENABLE_SCHEDULER", "true").lower() != "true":
        return
    try:
        from scheduler import start_scheduler
        start_scheduler(check_interval_minutes=60)
        logging.getLogger(__name__).info("Background scan scheduler started.")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Scheduler not started: {e}")


@app.before_request
def _start_scheduler_once():
    if not _scheduler_started:
        bootstrap_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.FLASK_PORT, debug=config.FLASK_DEBUG, threaded=True)
