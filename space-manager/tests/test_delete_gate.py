"""API-level tests for the deletion safety gate, with the iCloud session faked."""

import pytest
from fastapi.testclient import TestClient

from app.config import Account, Settings
from app.icloud import build_delete_payload
from app.main import CONFIRM_PHRASE, create_app


class FakeSession:
    """Stands in for AccountSession; records what would be deleted."""

    def __init__(self, account):
        self.account = account
        self.status = "ready"
        self.last_error = None
        self.scan_state = {"running": False, "album": None, "count": 0, "skipped": 0, "error": None}
        self.deleted = []

    def delete_assets(self, rows, dry_run):
        self.deleted.extend(rows)
        return [
            {"master_id": r["master_id"], "filename": r["filename"], "ok": True,
             "detail": "dry-run: would delete" if dry_run else "moved to Recently Deleted"}
            for r in rows
        ]


@pytest.fixture
def env(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    settings = Settings(
        accounts=(
            Account(
                name="j",
                apple_id="j@example.com",
                backup_dirs=(str(backup),),
                cookie_directory=str(tmp_path / "cookies"),
            ),
        ),
        db_path=str(tmp_path / "db.sqlite"),
        dry_run=False,
    )
    app = create_app(settings)
    fake = FakeSession(settings.accounts[0])
    app.state.sessions["j"] = fake
    store = app.state.store
    store.begin_scan("j", "Videos")
    store.add_assets(
        "j",
        "Videos",
        [
            {
                "master_id": "m1",
                "asset_record_name": "a1",
                "asset_change_tag": "t1",
                "filename": "VID_1.MOV",
                "size": 1000,
                "asset_date": "2024-01-01T00:00:00+00:00",
                "item_type": "movie",
            }
        ],
    )
    store.finish_scan("j")
    return TestClient(app), fake, backup


def test_delete_requires_confirmation_phrase(env):
    client, fake, backup = env
    res = client.post("/api/accounts/j/delete", json={"master_ids": ["m1"], "confirm": "yes"})
    assert res.status_code == 400
    assert fake.deleted == []


def test_delete_refused_without_verified_local_copy(env):
    client, fake, backup = env
    res = client.post(
        "/api/accounts/j/delete",
        json={"master_ids": ["m1"], "confirm": CONFIRM_PHRASE},
    )
    assert res.status_code == 409
    assert "no verified local copy" in res.json()["detail"]
    assert fake.deleted == []


def test_delete_refused_for_unknown_ids(env):
    client, fake, backup = env
    res = client.post(
        "/api/accounts/j/delete",
        json={"master_ids": ["nope"], "confirm": CONFIRM_PHRASE},
    )
    assert res.status_code == 400
    assert fake.deleted == []


def test_delete_proceeds_with_verified_copy_and_audits(env):
    client, fake, backup = env
    (backup / "VID_1.MOV").write_bytes(b"x" * 1000)
    res = client.post(
        "/api/accounts/j/delete",
        json={"master_ids": ["m1"], "confirm": CONFIRM_PHRASE},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["dry_run"] is False
    assert body["freed_bytes"] == 1000
    assert [r["master_id"] for r in fake.deleted] == ["m1"]
    # deleted asset leaves the cache
    assets = client.get("/api/accounts/j/assets").json()["rows"]
    assert assets == []
    audit = client.get("/api/accounts/j/audit").json()["entries"]
    assert audit and audit[0]["action"] == "delete"


def test_verified_status_reported_in_asset_list(env):
    client, fake, backup = env
    res = client.get("/api/accounts/j/assets").json()["rows"]
    assert res[0]["verified"] is False
    (backup / "VID_1.MOV").write_bytes(b"x" * 1000)
    res = client.get("/api/accounts/j/assets", params={"refresh_index": "true"}).json()["rows"]
    assert res[0]["verified"] is True


def test_dry_run_forced_by_settings(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "VID_1.MOV").write_bytes(b"x" * 1000)
    settings = Settings(
        accounts=(
            Account(
                name="j",
                apple_id="j@example.com",
                backup_dirs=(str(backup),),
                cookie_directory=str(tmp_path / "cookies"),
            ),
        ),
        db_path=str(tmp_path / "db.sqlite"),
        dry_run=True,
    )
    app = create_app(settings)
    fake = FakeSession(settings.accounts[0])
    app.state.sessions["j"] = fake
    app.state.store.begin_scan("j", "Videos")
    app.state.store.add_assets(
        "j",
        "Videos",
        [
            {
                "master_id": "m1",
                "asset_record_name": "a1",
                "asset_change_tag": "t1",
                "filename": "VID_1.MOV",
                "size": 1000,
                "asset_date": "2024-01-01T00:00:00+00:00",
                "item_type": "movie",
            }
        ],
    )
    client = TestClient(app)
    res = client.post(
        "/api/accounts/j/delete",
        json={"master_ids": ["m1"], "confirm": CONFIRM_PHRASE},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["dry_run"] is True
    assert body["freed_bytes"] == 0
    # asset stays in the cache after a dry run
    assert client.get("/api/accounts/j/assets").json()["rows"]


def test_delete_payload_matches_icloudpd_shape():
    payload = build_delete_payload("rec-name", "tag-1", {"zoneName": "PrimarySync"})
    assert payload["atomic"] is True
    op = payload["operations"][0]
    assert op["operationType"] == "update"
    assert op["record"]["recordType"] == "CPLAsset"
    assert op["record"]["fields"]["isDeleted"]["value"] == 1
    assert op["record"]["recordName"] == "rec-name"
    assert op["record"]["recordChangeTag"] == "tag-1"
    assert payload["zoneID"] == {"zoneName": "PrimarySync"}
