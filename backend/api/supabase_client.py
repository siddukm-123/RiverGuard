"""
Thin wrapper around the Supabase Python client.

If SUPABASE_URL / SUPABASE_KEY aren't set (e.g. first run before you've
created a project), every function here falls back to an in-memory store
so the app still boots and the dashboard still works for a local demo.
Once you add real credentials to .env, it transparently switches to
talking to your actual Supabase Postgres database.
"""
import logging
import threading
import time
from datetime import datetime, timezone
import uuid

from config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)

_client = None
_use_memory = False
_client_lock = threading.Lock()

# supabase-py talks HTTP/2 over ONE shared connection. When Supabase / a proxy closes an idle connection (or many
# threads hit it at once) httpx raises "RemoteProtocolError: Server disconnected". These errors are transient:
# rebuild the client and retry instead of returning a 500 to the browser.
_TRANSIENT_ERRORS = {"RemoteProtocolError", "ReadError", "WriteError", "ConnectError", "ConnectTimeout",
                     "ReadTimeout", "WriteTimeout", "PoolTimeout", "LocalProtocolError", "NetworkError",
                     "TimeoutException", "CloseError"}
_MAX_ATTEMPTS = 3

# ── in-memory fallback store (dev-only, resets on restart) ──────────────
_memory = {
    "rivers": [],
    "officers": [],
    "alerts": [],
    "detections": [],
    "evidence": [],
    "uploads": [],
}


def _seed_memory():
    if _memory["rivers"]:
        return
    seed_rivers = [
        {"name": "Cauvery", "state": "Tamil Nadu", "district": "Karur", "center_lat": 10.9601, "center_lng": 78.0766},
        {"name": "Brahmaputra", "state": "Assam", "district": "Guwahati", "center_lat": 26.1445, "center_lng": 91.7362},
        {"name": "Tungabhadra", "state": "Karnataka", "district": "Davangere", "center_lat": 14.4644, "center_lng": 75.9218},
    ]
    for r in seed_rivers:
        r["id"] = str(uuid.uuid4())
        r["buffer_geojson"] = None
        r["buffer_radius_m"] = 1000
        r["monitoring_enabled"] = True
        r["scan_interval_days"] = 5
        r["last_scanned_at"] = None
        r["monitor_points"] = None
        r["created_at"] = datetime.now(timezone.utc).isoformat()
        _memory["rivers"].append(r)


def get_client():
    """Returns a Supabase client, or None if running in memory-fallback mode."""
    global _client, _use_memory
    if _client is not None or _use_memory:
        return _client

    with _client_lock:
        if _client is not None or _use_memory:
            return _client

        if not SUPABASE_URL or not SUPABASE_KEY:
            logger.warning("SUPABASE_URL/SUPABASE_KEY not set — running with in-memory storage. "
                            "Set them in .env to persist data in your real Supabase project.")
            _use_memory = True
            _seed_memory()
            return None

        try:
            from supabase import create_client
            _client = create_client(SUPABASE_URL, SUPABASE_KEY)
            return _client
        except Exception as e:
            logger.error(f"Failed to create Supabase client, falling back to memory store: {e}")
            _use_memory = True
            _seed_memory()
            return None


def _is_transient(exc):
    return any(c.__name__ in _TRANSIENT_ERRORS for c in type(exc).__mro__)


def _drop_client(bad_client):
    """Forget a broken client so the next get_client() builds a fresh connection (first failing thread wins)."""
    global _client
    with _client_lock:
        if _client is bad_client:
            _client = None


def _exec(fn, on_retry=None):
    """Runs fn(client) with automatic retry + client rebuild on transient connection errors."""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        client = get_client()
        try:
            return fn(client)
        except Exception as e:  # noqa: BLE001
            if not _is_transient(e) or attempt == _MAX_ATTEMPTS:
                raise
            logger.warning(f"Supabase connection error ({e.__class__.__name__}: {e}); retry {attempt}/{_MAX_ATTEMPTS - 1}")
            _drop_client(client)
            time.sleep(0.25 * attempt)
            if on_retry:
                on_retry()


def table_select(table, filters=None, order_by=None, desc=False, limit=None):
    client = get_client()
    if client is None:
        rows = list(_memory[table])
        if filters:
            for k, v in filters.items():
                rows = [r for r in rows if r.get(k) == v]
        if order_by:
            rows.sort(key=lambda r: r.get(order_by, ""), reverse=desc)
        if limit:
            rows = rows[:limit]
        return rows

    def run(c):
        query = c.table(table).select("*")
        if filters:
            for k, v in filters.items():
                query = query.eq(k, v)
        if order_by:
            query = query.order(order_by, desc=desc)
        if limit:
            query = query.limit(limit)
        return query.execute().data
    return _exec(run)


def table_insert(table, row):
    client = get_client()
    row = dict(row)
    row.setdefault("id", str(uuid.uuid4()))
    row.setdefault("created_at", datetime.now(timezone.utc).isoformat())

    if client is None:
        _memory[table].append(row)
        return row

    retried = []

    def run(c):
        try:
            result = c.table(table).insert(row).execute()
        except Exception as e:  # noqa: BLE001
            # A retried insert may have actually succeeded the first time (same id) -> treat duplicate as success.
            if retried and ("23505" in str(e) or "duplicate key" in str(e).lower()):
                return None
            raise
        return result.data[0] if result.data else row
    return _exec(run, on_retry=lambda: retried.append(1)) or row


def table_insert_many(table, rows):
    """Batch insert (one round trip on Supabase). Returns the inserted rows."""
    if not rows:
        return []
    client = get_client()
    prepared = []
    for r in rows:
        r = dict(r)
        r.setdefault("id", str(uuid.uuid4()))
        r.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        prepared.append(r)
    if client is None:
        _memory[table].extend(prepared)
        return prepared
    retried = []

    def run(c):
        try:
            return c.table(table).insert(prepared).execute().data
        except Exception as e:  # noqa: BLE001
            if retried and ("23505" in str(e) or "duplicate key" in str(e).lower()):
                return None
            raise
    return _exec(run, on_retry=lambda: retried.append(1)) or prepared


def storage_mode():
    """'supabase' when a real database client is active, otherwise 'memory' (data lost on restart)."""
    get_client()
    return "memory" if _use_memory else "supabase"


def table_update(table, row_id, updates):
    client = get_client()
    if client is None:
        for r in _memory[table]:
            if r["id"] == row_id:
                r.update(updates)
                return r
        return None

    def run(c):
        result = c.table(table).update(updates).eq("id", row_id).execute()
        return result.data[0] if result.data else None
    return _exec(run)


def table_delete(table, row_id):
    client = get_client()
    if client is None:
        before = len(_memory[table])
        _memory[table] = [r for r in _memory[table] if r.get("id") != row_id]
        return before != len(_memory[table])

    _exec(lambda c: c.table(table).delete().eq("id", row_id).execute())
    return True


def table_delete_where(table, filters):
    client = get_client()
    if client is None:
        keys = set(filters.keys())
        before = len(_memory[table])
        _memory[table] = [r for r in _memory[table] if not all(r.get(k) == v for k, v in filters.items())]
        return before - len(_memory[table])

    def run(c):
        query = c.table(table).delete()
        for k, v in filters.items():
            query = query.eq(k, v)
        return query.execute()
    _exec(run)
    return True
