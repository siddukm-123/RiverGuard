"""
Background scheduler — checks every hour whether any monitored river is
due for its periodic satellite scan (default every 5 days, configurable
per river via `scan_interval_days`), and runs the full satellite pipeline
automatically if so. This is what makes monitoring "automatic" instead of
requiring someone to manually click a button every few days.

Started once from app.py. Uses APScheduler's BackgroundScheduler, which
runs in a separate thread inside the same Flask process — no separate
worker process needed for a project this size.
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_scheduler = None


def _due_for_scan(river):
    if not river.get("monitoring_enabled", True):
        return False
    interval_days = river.get("scan_interval_days") or 5
    last_scanned = river.get("last_scanned_at")
    if not last_scanned:
        return True
    try:
        last_dt = datetime.fromisoformat(last_scanned.replace("Z", "+00:00"))
    except Exception:
        return True
    return datetime.now(timezone.utc) - last_dt >= timedelta(days=interval_days)


def run_due_scans():
    """Checks every river and runs a satellite scan for any that are due."""
    from api.supabase_client import table_select, table_update
    import pipeline

    rivers = table_select("rivers")
    for river in rivers:
        if not _due_for_scan(river):
            continue
        try:
            logger.info(f"Scheduled scan due for river '{river['name']}' — running now.")
            after_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            before_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")

            alert = pipeline.process_satellite_scan(river, before_date, after_date)
            table_update("rivers", river["id"], {"last_scanned_at": datetime.now(timezone.utc).isoformat()})
            logger.info(f"Scan complete for '{river['name']}': {alert['severity']} ({alert['confidence']}%)")
        except Exception as e:
            logger.error(f"Scheduled scan failed for river '{river['name']}': {e}")


def start_scheduler(check_interval_minutes=60):
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.warning("apscheduler not installed — automatic periodic scans are disabled. "
                        "Install it (`pip install apscheduler`) to enable auto-monitoring.")
        return None

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(run_due_scans, "interval", minutes=check_interval_minutes, next_run_time=datetime.now())
    _scheduler.start()
    logger.info(f"Scheduler started — checking every {check_interval_minutes} min for rivers due a scan.")
    return _scheduler
