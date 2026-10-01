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
    from cratemover.web import server

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
    assert "Cratemover" in client.get("/").text
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
    assert [f["download"] for f in result["files"]] == ["xrb/rekordbox.xml"]
    download = client.get("/api/download", params={"path": result["files"][0]["download"]})
    assert download.status_code == 200 and b"DJ_PLAYLISTS" in download.content
    assert client.get("/api/download", params={"path": "../../etc/passwd"}).status_code == 403


def test_inspect_detects_the_format(client: TestClient, mixxx_root: Path) -> None:
    job = client.post("/api/inspect", json={"path": str(mixxx_root / "mixxx")}).json()
    assert _wait(client, job["job_id"])["format"] == "mixxx"
    assert client.post("/api/inspect", json={"path": str(mixxx_root / "nope")}).status_code == 400
    sources = client.get("/api/sources").json()["sources"]
    assert {"format": "mixxx", "path": str(mixxx_root / "mixxx" / "mixxxdb.sqlite")} in sources


def test_upload_rekordbox_xml(client: TestClient) -> None:
    xml = (Path(__file__).parent / "fixtures/rekordbox/rekordbox6-database.xml").read_bytes()
    path = client.post("/api/upload", files={"file": ("rekordbox.xml", xml)}).json()["path"]
    job = client.post("/api/inspect", json={"format": "rekordbox_xml", "path": path}).json()
    assert _wait(client, job["job_id"])["summary"]["tracks"] == 6


def test_sync_endpoint_preview_and_run(
    client: TestClient, library_copy: Path, tmp_path: Path
) -> None:
    import os

    stick = tmp_path / "export" / "STICK"
    stick.mkdir(parents=True)
    os.environ["BROWSE_ROOTS"] = f"{library_copy}:{tmp_path / 'export'}"
    from cratemover.web import server

    server.BROWSE_ROOTS[:] = [library_copy, tmp_path / "export"]
    body = {
        "a": {"format": "mixxx", "path": str(library_copy / "mixxx")},
        "b": {"format": "rekordbox_usb", "path": str(stick)},
        "direction": "a_to_b",
        "dry_run": True,
        "write": {"waveforms": False, "onelibrary": "off"},
    }
    # An empty stick can't be read yet: write it first through /api/convert.
    job = client.post("/api/inspect", json=body["a"]).json()
    lib = _wait(client, job["job_id"])
    job = client.post(
        "/api/convert",
        json={"library_id": lib["library_id"], "format": "rekordbox_usb", "target_path": str(stick),
              "waveforms": False, "playlists": ["Warm Up"]},
    )  # fmt: skip
    assert job.status_code == 200, job.text
    assert _wait(client, job.json()["job_id"])["in_place"] is True
    preview = _wait(client, client.post("/api/sync", json=body).json()["job_id"])
    assert preview["dry_run"] and preview["a_to_b"]["report"]["added"] == 3
    assert "written" not in preview["a_to_b"]
    body["dry_run"] = False
    done = _wait(client, client.post("/api/sync", json=body).json()["job_id"])
    assert done["a_to_b"]["written"]["summary"]["tracks"] >= 5
    outside = body | {"b": {"format": "rekordbox_usb", "path": "/etc"}}
    assert client.post("/api/sync", json=outside).status_code == 403


def test_drives_endpoint(client: TestClient) -> None:
    body = client.get("/api/drives").json()
    assert "drives" in body and body["roots"]


def test_sync_sends_only_the_chosen_playlists(library_copy: Path, tmp_path: Path) -> None:
    from cratemover.convert import (
        ReadOptions,
        SyncSide,
        WriteOptions,
        playlist_paths,
        read_library,
        sync_libraries,
        write_library,
    )
    from cratemover.sync import SyncOptions

    mixxx = ReadOptions("mixxx", str(library_copy / "mixxx"))
    source = read_library(mixxx)
    one = playlist_paths(source)[0]
    stick = tmp_path / "STICK"
    stick.mkdir()
    usb = WriteOptions("rekordbox_usb", str(stick), waveforms=False, usb_xml=False)
    write_library(source, WriteOptions(**{**usb.__dict__, "playlists": [one]}), [])

    def added(playlists: list[str]) -> int:
        result = sync_libraries(
            SyncSide(mixxx, WriteOptions("mixxx", "")),
            SyncSide(ReadOptions("rekordbox_usb", str(stick)), usb),
            "a_to_b",
            SyncOptions(),
            dry_run=True,
            playlists=playlists,
        )
        return result.a_to_b["report"]["added"]

    # Everything in the playlist is on the stick already (bar the Ogg file, stored as MP3).
    assert added([one]) <= 1
    assert added([]) > added([one])
