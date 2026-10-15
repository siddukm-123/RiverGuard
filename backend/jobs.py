"""
Tiny in-process job runner for slow work (video inference, multi-point satellite scans).

Why: a 2-3 minute request held open through the Vercel -> Railway proxy is fragile. The browser
now submits work, gets a job id immediately (HTTP 202) and polls /api/jobs/<id>.

Limits (documented in DEPLOY.md): state lives in this process. The Railway start command runs ONE
gunicorn worker, so polling always reaches the process that owns the job. If you scale to several
workers/replicas, jobs must move to a shared store (DB/queue). A restart loses in-flight jobs;
the poller then gets a 404 and shows a clear message.
"""
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rg-job")
_jobs = {}
_lock = threading.Lock()
_TTL_SECONDS = 3600


def _gc():
    cutoff = time.time() - _TTL_SECONDS
    for jid in [j for j, v in _jobs.items() if v.get("finished_at") and v["finished_at"] < cutoff]:
        _jobs.pop(jid, None)


def submit(kind, fn, *args, **kwargs):
    jid = uuid.uuid4().hex
    with _lock:
        _gc()
        _jobs[jid] = {"id": jid, "kind": kind, "status": "queued", "created_at": time.time(),
                      "started_at": None, "finished_at": None, "result": None, "error": None, "status_code": None}

    def run():
        with _lock:
            _jobs[jid].update(status="running", started_at=time.time())
        try:
            result = fn(*args, **kwargs)
            with _lock:
                _jobs[jid].update(status="done", result=result, finished_at=time.time(), status_code=200)
        except Exception as e:  # noqa: BLE001 - surfaced to the poller
            logger.exception(f"Job {kind}/{jid} failed")
            code = getattr(e, "http_status", 500)
            with _lock:
                _jobs[jid].update(status="error", error=str(e), finished_at=time.time(), status_code=code)

    _pool.submit(run)
    return jid


def get(jid):
    with _lock:
        j = _jobs.get(jid)
        return dict(j) if j else None
