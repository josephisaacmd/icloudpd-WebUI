from datetime import datetime, timedelta, timezone

from app.store import Store


def iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def row(i: int, size: int, days_ago: int = 0, item_type: str = "movie"):
    return {
        "master_id": f"m{i}",
        "asset_record_name": f"a{i}",
        "asset_change_tag": f"t{i}",
        "filename": f"VID_{i}.MOV",
        "size": size,
        "asset_date": iso(days_ago),
        "item_type": item_type,
    }


def make_store(tmp_path) -> Store:
    return Store(str(tmp_path / "test.db"))


def test_scan_replaces_previous_assets(tmp_path):
    store = make_store(tmp_path)
    store.begin_scan("j", "Videos")
    store.add_assets("j", "Videos", [row(1, 100)])
    store.finish_scan("j")
    store.begin_scan("j", "Videos")
    store.add_assets("j", "Videos", [row(2, 200)])
    store.finish_scan("j")
    assets = store.list_assets("j")
    assert [a["master_id"] for a in assets] == ["m2"]
    assert store.scan_info("j")["count"] == 1


def test_filters_and_sort(tmp_path):
    store = make_store(tmp_path)
    store.begin_scan("j", "Videos")
    store.add_assets(
        "j",
        "Videos",
        [
            row(1, 500, days_ago=400, item_type="movie"),
            row(2, 2000, days_ago=10, item_type="movie"),
            row(3, 1000, days_ago=400, item_type="image"),
        ],
    )
    store.finish_scan("j")

    largest = store.list_assets("j", sort="size")
    assert [a["master_id"] for a in largest] == ["m2", "m3", "m1"]

    movies = store.list_assets("j", item_type="movie")
    assert {a["master_id"] for a in movies} == {"m1", "m2"}

    big = store.list_assets("j", min_size=900)
    assert {a["master_id"] for a in big} == {"m2", "m3"}

    old = store.list_assets("j", older_than_days=100)
    assert {a["master_id"] for a in old} == {"m1", "m3"}

    named = store.list_assets("j", name_contains="VID_2")
    assert [a["master_id"] for a in named] == ["m2"]


def test_accounts_are_isolated(tmp_path):
    store = make_store(tmp_path)
    store.begin_scan("j", "Videos")
    store.add_assets("j", "Videos", [row(1, 100)])
    store.begin_scan("c", "Videos")
    store.add_assets("c", "Videos", [row(2, 100)])
    assert [a["master_id"] for a in store.list_assets("j")] == ["m1"]
    assert [a["master_id"] for a in store.list_assets("c")] == ["m2"]


def test_get_and_remove_assets(tmp_path):
    store = make_store(tmp_path)
    store.begin_scan("j", "Videos")
    store.add_assets("j", "Videos", [row(1, 100), row(2, 200)])
    got = store.get_assets("j", ["m1", "m2", "m3"])
    assert {g["master_id"] for g in got} == {"m1", "m2"}
    store.remove_assets("j", ["m1"])
    assert [a["master_id"] for a in store.list_assets("j")] == ["m2"]


def test_audit_log(tmp_path):
    store = make_store(tmp_path)
    store.audit("j", "m1", "VID_1.MOV", 100, "delete", "ok: moved")
    entries = store.audit_entries("j")
    assert len(entries) == 1
    assert entries[0]["action"] == "delete"
