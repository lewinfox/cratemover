from __future__ import annotations

import importlib
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(mixxx_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "export"))
    monkeypatch.setenv("BROWSE_ROOTS", f"{mixxx_root}:{tmp_path / 'export'}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    from djconvert.web import server

    importlib.reload(server)
    return TestClient(server.app)


def _wait(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            assert job["status"] == "done", job
            return job["result"]
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_index_and_info(client: TestClient, mixxx_root: Path) -> None:
    assert "DJ Library Converter" in client.get("/").text
    info = client.get("/api/info").json()
    assert {"format": "mixxx", "path": str(mixxx_root / "mixxx" / "mixxxdb.sqlite")} in info[
        "suggestions"
    ]


def test_browse_is_restricted(client: TestClient, mixxx_root: Path) -> None:
    assert client.get("/api/browse", params={"path": str(mixxx_root)}).status_code == 200
    assert client.get("/api/browse", params={"path": "/etc"}).status_code == 403


def test_inspect_convert_download(client: TestClient, mixxx_root: Path) -> None:
    job = client.post(
        "/api/inspect", json={"format": "mixxx", "path": str(mixxx_root / "mixxx")}
    ).json()
    lib = _wait(client, job["job_id"])
    assert lib["summary"]["tracks"] == 6 and lib["summary"]["missing_files"] == 1
    tracks = client.get(
        f"/api/libraries/{lib['library_id']}/tracks", params={"playlist": "Warm Up"}
    ).json()
    assert tracks["total"] == 3
    job = client.post(
        "/api/convert",
        json={"library_id": lib["library_id"], "format": "rekordbox_xml", "output_name": "../x/rb"},
    ).json()
    result = _wait(client, job["job_id"])
    assert result["files"] == ["xrb/rekordbox.xml"]
    download = client.get("/api/download", params={"path": result["files"][0]})
    assert download.status_code == 200 and b"DJ_PLAYLISTS" in download.content
    assert client.get("/api/download", params={"path": "../../etc/passwd"}).status_code == 403


def test_upload_rekordbox_xml(client: TestClient) -> None:
    xml = (Path(__file__).parent / "fixtures/rekordbox/rekordbox6-database.xml").read_bytes()
    path = client.post("/api/upload", files={"file": ("rekordbox.xml", xml)}).json()["path"]
    job = client.post("/api/inspect", json={"format": "rekordbox_xml", "path": path}).json()
    assert _wait(client, job["job_id"])["summary"]["tracks"] == 6
