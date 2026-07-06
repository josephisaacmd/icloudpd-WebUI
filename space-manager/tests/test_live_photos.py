"""Live Photo verification: both the still and the motion clip must be
present and byte-identical before the asset is deletable."""

import pytest
from fastapi.testclient import TestClient

from app.config import Account, Settings
from app.main import CONFIRM_PHRASE, create_app
from app.verify import build_index, verify_asset

from .test_delete_gate import FakeSession


def make(tmp_path, rel, size):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return str(path)


# -- verify_asset ------------------------------------------------------------


def test_live_photo_verified_with_hevc_suffix_clip(tmp_path):
    make(tmp_path, "IMG_1.HEIC", 1000)
    make(tmp_path, "IMG_1_HEVC.MOV", 300)  # default "suffix" policy
    index = build_index([str(tmp_path)])
    ok, still, detail = verify_asset(index, "IMG_1.HEIC", 1000, 300)
    assert ok and still and detail == "ok"


def test_live_photo_verified_with_original_policy_clip(tmp_path):
    make(tmp_path, "IMG_1.HEIC", 1000)
    make(tmp_path, "IMG_1.MOV", 300)  # "original" policy naming
    index = build_index([str(tmp_path)])
    ok, _, _ = verify_asset(index, "IMG_1.HEIC", 1000, 300)
    assert ok


def test_live_photo_jpeg_still_uses_plain_mov_name(tmp_path):
    make(tmp_path, "IMG_1.JPG", 1000)
    make(tmp_path, "IMG_1.MOV", 300)
    index = build_index([str(tmp_path)])
    ok, _, _ = verify_asset(index, "IMG_1.JPG", 1000, 300)
    assert ok


def test_live_photo_missing_motion_clip_is_refused(tmp_path):
    make(tmp_path, "IMG_1.HEIC", 1000)
    index = build_index([str(tmp_path)])
    ok, still, detail = verify_asset(index, "IMG_1.HEIC", 1000, 300)
    assert not ok
    assert still is not None  # still was found — the clip is what's missing
    assert "motion clip" in detail


def test_live_photo_motion_clip_size_mismatch_is_refused(tmp_path):
    make(tmp_path, "IMG_1.HEIC", 1000)
    make(tmp_path, "IMG_1_HEVC.MOV", 299)
    index = build_index([str(tmp_path)])
    ok, _, _ = verify_asset(index, "IMG_1.HEIC", 1000, 300)
    assert not ok


def test_live_photo_dedup_suffixed_clip_matches(tmp_path):
    make(tmp_path, "IMG_1.HEIC", 1000)
    make(tmp_path, "IMG_1_HEVC-300.MOV", 300)  # dedup suffix on the clip
    index = build_index([str(tmp_path)])
    ok, _, _ = verify_asset(index, "IMG_1.HEIC", 1000, 300)
    assert ok


def test_plain_image_ignores_lp_size_none(tmp_path):
    make(tmp_path, "IMG_1.HEIC", 1000)
    index = build_index([str(tmp_path)])
    ok, _, _ = verify_asset(index, "IMG_1.HEIC", 1000, None)
    assert ok


# -- API gate ------------------------------------------------------------------


LIVE_ROW = {
    "master_id": "m1",
    "asset_record_name": "a1",
    "asset_change_tag": "t1",
    "filename": "IMG_1.HEIC",
    "size": 1000,
    "lp_size": 300,
    "asset_date": "2024-01-01T00:00:00+00:00",
    "item_type": "image",
}


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
    app.state.store.begin_scan("j", "All Photos")
    app.state.store.add_assets("j", "All Photos", [LIVE_ROW])
    app.state.store.finish_scan("j")
    return TestClient(app), fake, backup


def test_delete_refused_when_only_still_is_backed_up(env):
    client, fake, backup = env
    (backup / "IMG_1.HEIC").write_bytes(b"x" * 1000)
    res = client.post(
        "/api/accounts/j/delete",
        json={"master_ids": ["m1"], "confirm": CONFIRM_PHRASE},
    )
    assert res.status_code == 409
    assert "motion clip" in res.json()["detail"]
    assert fake.deleted == []


def test_delete_allowed_with_both_parts_and_counts_both_sizes(env):
    client, fake, backup = env
    (backup / "IMG_1.HEIC").write_bytes(b"x" * 1000)
    (backup / "IMG_1_HEVC.MOV").write_bytes(b"x" * 300)
    res = client.post(
        "/api/accounts/j/delete",
        json={"master_ids": ["m1"], "confirm": CONFIRM_PHRASE},
    )
    assert res.status_code == 200
    assert res.json()["freed_bytes"] == 1300
    assert [r["master_id"] for r in fake.deleted] == ["m1"]


def test_asset_list_reports_motion_clip_status(env):
    client, fake, backup = env
    (backup / "IMG_1.HEIC").write_bytes(b"x" * 1000)
    rows = client.get("/api/accounts/j/assets", params={"refresh_index": "true"}).json()["rows"]
    assert rows[0]["verified"] is False
    assert "motion clip" in rows[0]["verify_detail"]
    assert rows[0]["total_size"] == 1300
    (backup / "IMG_1_HEVC.MOV").write_bytes(b"x" * 300)
    rows = client.get("/api/accounts/j/assets", params={"refresh_index": "true"}).json()["rows"]
    assert rows[0]["verified"] is True
