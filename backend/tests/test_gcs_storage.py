"""GCS media storage + Supabase retry tests. A fake in-memory bucket replaces Google Cloud (no network, no credentials)."""
import uuid
from pathlib import Path

import httpx
import pytest

import config
import gcs_storage
from api import supabase_client


class NotFound(Exception):  # class name matters: gcs_storage treats "NotFound" as a quiet miss
    pass


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name, self.content_type = store, name, None

    def upload_from_filename(self, filename, content_type=None, timeout=None):
        self.store[self.name] = (Path(filename).read_bytes(), content_type)

    def upload_from_string(self, data, content_type=None, timeout=None):
        self.store[self.name] = (data, content_type)

    def download_as_bytes(self, timeout=None):
        if self.name not in self.store:
            raise NotFound(self.name)
        data, ctype = self.store[self.name]
        self.content_type = ctype
        return data

    def delete(self):
        self.store.pop(self.name, None)


class FakeBucket:
    def __init__(self):
        self.store = {}

    def blob(self, name):
        return FakeBlob(self.store, name)


@pytest.fixture()
def fake_gcs(monkeypatch):
    bucket = FakeBucket()
    monkeypatch.setattr(config, "GCS_BUCKET", "imagesmap")
    monkeypatch.setattr(gcs_storage, "_bucket", bucket)
    return bucket


def _jpg(tmp_path, name="x.jpg", payload=b"\xff\xd8\xff-jpeg-bytes"):
    p = tmp_path / name
    p.write_bytes(payload)
    return str(p)


def test_disabled_by_default_keeps_local_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GCS_BUCKET", "")
    media = {"annotated": _jpg(tmp_path)}
    assert gcs_storage.upload_media("abc", media) == media
    assert gcs_storage.upload_pdf("abc", _jpg(tmp_path, "e.pdf")) is None


def test_upload_media_replaces_paths_with_refs_and_skips_video(tmp_path, fake_gcs):
    aid = str(uuid.uuid4())
    media = {"original": _jpg(tmp_path, "up.png"), "annotated": _jpg(tmp_path, "a.jpg"),
             "comparison": _jpg(tmp_path, "c.jpg")}
    out = gcs_storage.upload_media(aid, media)
    assert out["annotated"] == f"gs://imagesmap/alerts/{aid}/annotated.jpg"
    assert out["original"] == f"gs://imagesmap/alerts/{aid}/original.png"  # keeps the extension (API filters on it)
    assert f"alerts/{aid}/comparison.jpg" in fake_gcs.store

    video = gcs_storage.upload_media(aid, {"original": _jpg(tmp_path, "clip.mp4"), "annotated": _jpg(tmp_path)})
    assert video["original"].endswith("clip.mp4") and not gcs_storage.is_ref(video["original"])  # never uploaded


def test_upload_failure_falls_back_to_local_path(tmp_path, monkeypatch, fake_gcs):
    def boom(*a, **k):
        raise RuntimeError("403 forbidden")
    monkeypatch.setattr(FakeBlob, "upload_from_filename", boom)
    p = _jpg(tmp_path)
    assert gcs_storage.upload_media("abc", {"annotated": p}) == {"annotated": p}


def test_ref_of_another_bucket_is_never_read(fake_gcs):
    assert gcs_storage.download_ref("gs://someone-elses-bucket/secret.jpg") is None


def test_api_serves_images_from_bucket_without_touching_the_database(client, fake_gcs, monkeypatch):
    aid = str(uuid.uuid4())
    fake_gcs.store[f"alerts/{aid}/before.jpg"] = (b"BEFORE", "image/jpeg")
    fake_gcs.store[f"alerts/{aid}/annotated.jpg"] = (b"ANNOT", "image/jpeg")
    import api.routes as routes

    def no_db(*a, **k):
        raise AssertionError("the database must not be queried for bucket images")
    monkeypatch.setattr(routes, "table_select", no_db)

    r = client.get(f"/api/alerts/{aid}/before.jpg")
    assert r.status_code == 200 and r.data == b"BEFORE" and r.mimetype == "image/jpeg"
    assert "max-age" in r.headers["Cache-Control"]
    assert client.get(f"/api/alerts/{aid}/image/annotated").data == b"ANNOT"
    assert client.get(f"/api/alerts/{aid}/annotated.jpg").data == b"ANNOT"


def test_api_resolves_gs_refs_stored_in_the_database(client, fake_gcs):
    from api.supabase_client import table_insert
    aid = str(uuid.uuid4())
    fake_gcs.store[f"alerts/{aid}/original.png"] = (b"PNGDATA", "image/png")
    fake_gcs.store[f"alerts/{aid}/evidence.pdf"] = (b"%PDF-fake", "application/pdf")
    table_insert("alerts", {
        "id": aid, "river_id": None, "source": "manual_upload", "severity": "SAFE", "confidence": 1,
        "reasoning_text": "t", "detected_at": "2026-01-01T00:00:00+00:00",
        "reasoning_signals": {"media": {"original": f"gs://imagesmap/alerts/{aid}/original.png"}},
        "evidence_pdf_url": f"gs://imagesmap/alerts/{aid}/evidence.pdf",
    })
    a = client.get(f"/api/alerts/{aid}").get_json()
    assert "original" in a["reasoning_signals"]["media_available"]  # extension check still works with gs:// refs
    assert a["evidence_pdf_url"] == f"/api/alerts/{aid}/evidence.pdf"
    r = client.get(f"/api/alerts/{aid}/image/original")
    assert r.status_code == 200 and r.data == b"PNGDATA"
    pdf = client.get(f"/api/alerts/{aid}/evidence.pdf")
    assert pdf.status_code == 200 and pdf.data == b"%PDF-fake"
    assert "attachment" in pdf.headers["Content-Disposition"]


def test_missing_bucket_object_gives_clean_404(client, fake_gcs):
    r = client.get(f"/api/alerts/{uuid.uuid4()}/before.jpg")
    assert r.status_code == 404 and r.get_json()["code"] in ("not_found", "file_missing")


def test_system_status_reports_media_storage_and_probe(client, fake_gcs):
    st = client.get("/api/system-status?probe=1").get_json()
    assert st["media_storage"] == "gcs" and st["gcs_probe"]["ok"] is True
    assert "_riverguard_probe/ping.txt" not in fake_gcs.store  # probe cleans up after itself


def test_system_status_without_gcs(client, monkeypatch):
    monkeypatch.setattr(config, "GCS_BUCKET", "")
    st = client.get("/api/system-status").get_json()
    assert st["media_storage"].startswith("local_disk") and st["gcs_probe"] is None


# ── Supabase retry on "Server disconnected" ─────────────────────────────
class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, owner):
        self.owner = owner

    def select(self, *a):
        return self

    def eq(self, *a):
        return self

    def insert(self, row):
        self.row = row
        return self

    def execute(self):
        self.owner.calls += 1
        if self.owner.calls <= self.owner.fail_times:
            raise httpx.RemoteProtocolError("Server disconnected")
        return _Result(getattr(self, "row", None) and [self.row] or [{"id": "1"}])


class _FlakyClient:
    def __init__(self, fail_times):
        self.fail_times, self.calls = fail_times, 0

    def table(self, name):
        return _Query(self)


def _use_flaky(monkeypatch, fail_times):
    holder = _FlakyClient(fail_times)
    monkeypatch.setattr(supabase_client, "_use_memory", False)
    monkeypatch.setattr(supabase_client, "_client", holder)
    monkeypatch.setattr(supabase_client, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    # after a drop the module must "reconnect": hand back the same fake so we can count calls
    monkeypatch.setattr(supabase_client, "get_client", lambda: holder)
    return holder


def test_select_retries_after_server_disconnected(monkeypatch):
    c = _use_flaky(monkeypatch, fail_times=2)
    assert supabase_client.table_select("alerts", filters={"id": "1"}) == [{"id": "1"}]
    assert c.calls == 3


def test_select_gives_up_after_three_attempts(monkeypatch):
    _use_flaky(monkeypatch, fail_times=99)
    with pytest.raises(httpx.RemoteProtocolError):
        supabase_client.table_select("alerts")


def test_non_transient_errors_are_not_retried(monkeypatch):
    holder = _use_flaky(monkeypatch, fail_times=0)

    class Boom(_Query):
        def execute(self):
            holder.calls += 1
            raise ValueError("bad column")
    monkeypatch.setattr(holder, "table", lambda n: Boom(holder), raising=False)
    with pytest.raises(ValueError):
        supabase_client.table_select("alerts")
    assert holder.calls == 1


def test_insert_retry_keeps_same_id(monkeypatch):
    c = _use_flaky(monkeypatch, fail_times=1)
    row = supabase_client.table_insert("alerts", {"id": "fixed-id", "severity": "SAFE"})
    assert row["id"] == "fixed-id" and c.calls == 2
