"""/api/metrics: 'rivers_scanned' = rivers with at least one completed satellite scan."""
from datetime import datetime, timezone

from api.supabase_client import table_select, table_update


def test_rivers_scanned_counts_rivers_that_have_a_last_scan_time(client):
    before = client.get("/api/metrics").get_json()
    assert {"rivers_monitored", "high_severity", "watch_severity", "total_alerts", "rivers_scanned"} <= set(before)
    assert before["rivers_scanned"] == len([r for r in table_select("rivers") if r.get("last_scanned_at")])
    assert before["rivers_scanned"] <= before["rivers_monitored"]

    never_scanned = [r for r in table_select("rivers") if not r.get("last_scanned_at")]
    assert never_scanned, "test needs at least one river that was never scanned"
    table_update("rivers", never_scanned[0]["id"], {"last_scanned_at": datetime.now(timezone.utc).isoformat()})

    after = client.get("/api/metrics").get_json()
    assert after["rivers_scanned"] == before["rivers_scanned"] + 1
    assert after["rivers_monitored"] == before["rivers_monitored"]      # adding a scan never adds a river
