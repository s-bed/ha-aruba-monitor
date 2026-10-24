from pathlib import Path

import pytest
from conftest import ap, client, inventory

from custom_components.aruba_monitor.store import Store


def campaign(store):
    return store.campaigns()[0]


@pytest.mark.parametrize("away_ssid", ["New Wi-Fi", "Guest Wi-Fi", "old wi-fi", "Old Wi-Fi "])
def test_source_offline_other_ssid_return_recovery(store, time_at, away_ssid):
    store.ingest([client()], [ap()], inventory(), time_at(0))
    store.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    store.ingest([], [ap(clients=0)], inventory(), time_at(2))
    assert campaign(store)["counts"]["pending"] == 1
    assert campaign(store)["clients"][0]["connection"] == "offline"
    store.ingest([client(ssid=away_ssid)], [ap()], inventory(), time_at(3))
    assert campaign(store)["ready"]
    observed = campaign(store)["clients"][0]
    assert observed["first_away_seen"] == time_at(3)
    assert observed["last_away_seen"] == time_at(3)
    assert observed["first_target_seen"] == (time_at(3) if away_ssid == "New Wi-Fi" else None)
    store.ingest([], [ap(clients=0)], inventory(), time_at(4))
    assert campaign(store)["counts"]["confirmed"] == 1
    assert campaign(store)["ready"]
    store.ingest([client()], [ap()], inventory(), time_at(5))
    assert campaign(store)["counts"]["returned"] == 1
    assert not campaign(store)["ready"]
    store.ingest([client(ssid=away_ssid)], [ap()], inventory(), time_at(6))
    assert campaign(store)["ready"]
    assert campaign(store)["clients"][0]["first_away_seen"] == time_at(3)
    assert campaign(store)["clients"][0]["last_away_seen"] == time_at(6)


def test_new_source_clients_enroll_automatically(store, time_at):
    store.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(0))
    assert not campaign(store)["ready"]
    store.ingest([client()], [ap()], inventory(), time_at(1))
    assert campaign(store)["counts"]["pending"] == 1


@pytest.mark.parametrize("away_ssid", ["New Wi-Fi", "Guest Wi-Fi"])
def test_historical_other_ssid_does_not_confirm_new_campaign(store, time_at, away_ssid):
    store.ingest([client()], [ap()], inventory(), time_at(0))
    store.ingest([client(ssid=away_ssid)], [ap()], inventory(), time_at(1))
    store.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(2))
    assert campaign(store)["counts"]["pending"] == 1


@pytest.mark.parametrize("away_ssid", ["New Wi-Fi", "Guest Wi-Fi"])
def test_manual_link_unlink_recalculates_preserving_evidence(store, time_at, away_ssid):
    store.ingest([client()], [ap()], inventory(), time_at(0))
    store.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    old_device = store.clients()[0]["id"]
    new_mac = "aa:bb:cc:00:00:99"
    store.ingest([client(mac=new_mac, ssid=away_ssid)], [ap()], inventory(), time_at(2))
    assert campaign(store)["counts"]["pending"] == 1
    store.link(old_device, new_mac)
    assert len(store.clients()) == 1
    assert campaign(store)["counts"]["confirmed"] == 1
    assert len(store.clients()[0]["sightings"]) == 2
    store.unlink(old_device, new_mac)
    assert len(store.clients()) == 2
    assert campaign(store)["counts"]["pending"] == 1
    store.link(old_device, new_mac)
    assert campaign(store)["ready"]


@pytest.mark.parametrize("away_ssid", ["New Wi-Fi", "Guest Wi-Fi"])
def test_other_ssid_evidence_kept_before_source_enrollment(store, time_at, away_ssid):
    store.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(0))
    store.ingest([client(ssid=away_ssid)], [ap()], inventory(), time_at(1))
    store.ingest([client()], [ap()], inventory(), time_at(2))
    assert campaign(store)["counts"]["returned"] == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_simultaneous_source_and_other_ssid_remains_returned(store, time_at, reverse):
    store.ingest([client()], [ap()], inventory(), time_at(0))
    store.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    device = store.clients()[0]["id"]
    other = client(mac="aa:bb:cc:00:00:99", ssid="Guest Wi-Fi")
    store.ingest([other], [ap()], inventory(), time_at(2))
    store.link(device, other["mac"])
    assert campaign(store)["counts"]["confirmed"] == 1
    observations = [client(), other]
    store.ingest(observations[::-1] if reverse else observations, [ap()], inventory(), time_at(3))
    assert campaign(store)["counts"]["returned"] == 1
    assert not campaign(store)["ready"]
    store.ingest([other], [ap()], inventory(), time_at(4))
    assert campaign(store)["ready"]


def test_latest_of_multiple_other_ssids_determines_confirmation(store, time_at):
    store.ingest([client()], [ap()], inventory(), time_at(0))
    store.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    store.ingest([client(ssid="New Wi-Fi")], [ap()], inventory(), time_at(2))
    store.ingest([client()], [ap()], inventory(), time_at(3))
    assert campaign(store)["counts"]["returned"] == 1
    store.ingest([client(ssid="Guest Wi-Fi")], [ap()], inventory(), time_at(4))
    detail = campaign(store)
    assert detail["ready"]
    assert detail["progress_pct"] == 100
    assert detail["clients"][0]["first_target_seen"] == time_at(2)
    assert detail["clients"][0]["last_target_seen"] == time_at(2)
    assert detail["clients"][0]["first_away_seen"] == time_at(2)
    assert detail["clients"][0]["last_away_seen"] == time_at(4)


@pytest.mark.parametrize("away_ssid", ["New Wi-Fi", "Guest Wi-Fi"])
def test_completed_campaign_evidence_stays_frozen(store, time_at, away_ssid):
    store.ingest([client()], [ap()], inventory(), time_at(0))
    cid = store.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    store.ingest([client(ssid=away_ssid)], [ap()], inventory(), time_at(2))
    store.complete(cid)
    before = [tuple(row) for row in store.db.execute("SELECT * FROM evidence")]
    store.ingest([client()], [ap()], inventory(), time_at(3))
    store.ingest([client(ssid="Another Wi-Fi")], [ap()], inventory(), time_at(4))
    assert campaign(store)["counts"]["confirmed"] == 1
    assert campaign(store)["clients"][0]["last_away_seen"] == time_at(2)
    assert [tuple(row) for row in store.db.execute("SELECT * FROM evidence")] == before


@pytest.mark.parametrize("away_ssid", ["New Wi-Fi", "Guest Wi-Fi"])
def test_exclusion_restore_and_completion(store, time_at, away_ssid):
    store.ingest([client(), client(mac="aa:bb:cc:00:00:02")], [ap()], inventory(), time_at(0))
    cid = store.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    device = next(c for c in store.clients() if c["mac"].endswith("02"))["id"]
    store.ingest([client(ssid=away_ssid)], [ap()], inventory(), time_at(2))
    store.exclude(cid, device, True, "Retired plug")
    assert campaign(store)["progress_pct"] == 100
    assert campaign(store)["counts"]["excluded"] == 1
    store.exclude(cid, device, False, "")
    assert not campaign(store)["ready"]
    with pytest.raises(ValueError, match="fresh collection"):
        store.complete(cid)
    store.exclude(cid, device, True, "Retired plug")
    store.complete(cid)
    store.ingest([client(mac="aa:bb:cc:00:00:03")], [ap()], inventory(), time_at(3))
    assert campaign(store)["total"] == 2  # Completed campaign freezes enrollment.
    assert campaign(store)["completed_at"]
    with pytest.raises(ValueError, match="read-only"):
        store.exclude(cid, device, False, "")


def test_all_excluded_is_not_ready(store, time_at):
    store.ingest([client()], [ap()], inventory(), time_at(0))
    cid = store.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    store.exclude(cid, store.clients()[0]["id"], True, "Retired")
    assert not campaign(store)["ready"]


def test_failure_preserves_snapshot_and_blocks_completion(store, time_at):
    store.ingest([client()], [ap()], inventory(), time_at(0))
    store.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    store.ingest([client(ssid="New Wi-Fi")], [ap()], inventory(), time_at(2))
    store.failure("Controller unreachable")
    assert not campaign(store)["ready"]
    assert store.clients()[0]["connection"] == "unknown"
    assert store.network()["aps"][0]["online"]
    assert store.network()["total_clients"] == 1
    assert store.status()["last_success"] == time_at(2)
    assert store.status(now=time_at(93))["stale"]


def test_missing_ap_only_offline_after_successful_poll(store, time_at):
    store.ingest([], [ap()], inventory(), time_at(0))
    store.failure("Error")
    assert store.network()["aps"][0]["online"]
    store.ingest([], [], inventory(), time_at(1))
    assert not store.network()["aps"][0]["online"]


def test_inventory_includes_zero_client_ssid(store):
    store.ingest([client()], [ap()], inventory())
    empty = next(s for s in store.network()["ssids"] if s["ssid"] == "Empty guest")
    assert empty["clients"] == 0 and empty["in_inventory"]
    assert store.network()["ssids"][2]["by_band"] == {"unknown": 1}


@pytest.mark.parametrize("away_ssid", ["New Wi-Fi", "Guest Wi-Fi"])
def test_restart_preserves_campaign_aliases_labels_and_history(tmp_path, time_at, away_ssid):
    path = str(tmp_path / "state.sqlite3")
    first = Store(path)
    first.ingest([client()], [ap()], inventory(), time_at(0))
    first.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    device = first.clients()[0]["id"]
    first.label(device, "My speaker")
    first.ingest([client(ssid=away_ssid)], [ap()], inventory(), time_at(2))
    first.close()
    second = Store(path)
    assert second.clients()[0]["label"] == "My speaker"
    assert second.clients()[0]["id"] == device
    assert campaign(second)["counts"]["confirmed"] == 1
    assert len(second.clients()[0]["sightings"]) == 2
    second.close()


def test_snapshot_transaction_rolls_back_on_storage_error(store):
    store.ingest([client()], [ap()], inventory())
    before = store.status()["last_success"]
    with pytest.raises(KeyError):
        store.ingest([client(mac="aa:bb:cc:00:00:02")], [{}], inventory())
    assert len(store.clients()) == 1
    assert store.status()["last_success"] == before


def test_source_target_must_differ(store):
    with pytest.raises(ValueError, match="differ"):
        store.create_campaign("Rotation", "Same", "Same")


def test_delete_campaign_only_removes_its_evidence_members_and_exclusions(store, time_at):
    store.ingest([client()], [ap()], inventory(), time_at(0))
    remove_id = store.create_campaign("Remove", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    keep_id = store.create_campaign("Keep", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    device = store.clients()[0]["id"]
    store.label(device, "Keep this label")
    store.exclude(remove_id, device, True, "Retired")
    store.ingest([client(ssid="New Wi-Fi")], [ap()], inventory(), time_at(2))
    clients_before = store.clients()
    campaign_before = next(c for c in store.campaigns() if c["id"] == keep_id)
    store.delete_campaign(remove_id)
    assert store.campaigns() == [campaign_before]
    assert store.clients() == clients_before
    for table in ("members", "evidence"):
        assert (
            store.db.execute(f"SELECT COUNT(*) FROM {table} WHERE campaign_id=?", (remove_id,)).fetchone()[0]
            == 0
        )
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    store.ingest([client()], [ap()], inventory(), time_at(3))
    assert [c["id"] for c in store.campaigns()] == [keep_id]


def test_campaign_delete_is_atomic(store, time_at):
    import sqlite3

    store.ingest([client()], [ap()], inventory(), time_at(0))
    cid = store.create_campaign("Keep on failure", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    store.ingest([client()], [ap()], inventory(), time_at(2))
    before = store.campaigns()
    store.db.executescript("""CREATE TEMP TRIGGER abort_campaign_delete BEFORE DELETE ON campaigns
        BEGIN SELECT RAISE(ABORT, 'test delete failure'); END;""")
    with pytest.raises(sqlite3.IntegrityError, match="test delete failure"):
        store.delete_campaign(cid)
    assert store.campaigns() == before
    assert store.db.execute("SELECT COUNT(*) FROM evidence WHERE campaign_id=?", (cid,)).fetchone()[0] == 1


@pytest.mark.parametrize("version", [0, 2, 99])
def test_unsupported_schema_is_not_initialized_or_relabelled(tmp_path, version):
    import sqlite3

    path = tmp_path / "unsupported.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE keep_me (value TEXT)")
        db.execute("INSERT INTO keep_me VALUES ('preserve')")
        db.execute(f"PRAGMA user_version={version}")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="schema version"):
        Store(str(path))
    assert path.read_bytes() == before


def test_schema_version_one_with_missing_tables_is_rejected(tmp_path):
    import sqlite3

    path = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("PRAGMA user_version=1")
    with pytest.raises(ValueError, match="missing tables"):
        Store(str(path))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("meta",)]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_schema_version_one_with_missing_columns_is_rejected(tmp_path):
    import sqlite3

    path = str(tmp_path / "missing-column.sqlite3")
    original = Store(path)
    original.close()
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE devices DROP COLUMN label")
    with pytest.raises(ValueError, match="devices table"):
        Store(path)


def test_schema_version_one_with_invalid_relationships_is_rejected(tmp_path):
    import sqlite3

    path = str(tmp_path / "invalid-relationships.sqlite3")
    original = Store(path)
    original.close()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO aliases VALUES ('aa', 'missing', 'first', 'last', '{}')")
    with pytest.raises(ValueError, match="invalid relationships"):
        Store(path)


def test_live_collection_required_after_restart_blocks_completion(tmp_path, time_at):
    path = str(tmp_path / "imported.sqlite3")
    first = Store(path)
    first.ingest([client()], [ap()], inventory(), time_at(0))
    cid = first.create_campaign("Rotation", "Old Wi-Fi", "New Wi-Fi", time_at(1))
    first.ingest([client(ssid="New Wi-Fi")], [ap()], inventory(), time_at(2))
    assert first.campaigns()[0]["ready"]
    first.close()
    second = Store(path, require_live_collection=True)
    try:
        assert second.status()["last_success"] == time_at(2)
        assert not second.status()["live_collection"]
        assert second.status()["stale"]
        assert second.clients()[0]["connection"] == "unknown"
        assert second.campaigns()[0]["counts"]["confirmed"] == 1
        with pytest.raises(ValueError, match="fresh collection"):
            second.complete(cid)
        second.failure("Controller unreachable")
        assert not second.status()["live_collection"]
        with pytest.raises(KeyError):
            second.ingest([], [{}], inventory())
        assert not second.status()["live_collection"]
        second.ingest([client(ssid="New Wi-Fi")], [ap()], inventory(), time_at(3))
        assert second.status()["live_collection"]
        assert second.status()["fresh"]
        second.complete(cid)
    finally:
        second.close()


def test_snapshot_has_atomic_flat_contract_and_checkpoint_retains_committed_data(tmp_path):
    import sqlite3

    path = str(tmp_path / "snapshot.sqlite3")
    store = Store(path, require_live_collection=True)
    try:
        store.ingest([client()], [ap()], inventory())
        snapshot = store.snapshot()
        assert set(snapshot) == {
            "status",
            "clients",
            "client_connections",
            "campaigns",
            "aps",
            "ssids",
            "total_clients",
        }
        assert snapshot["status"]["fresh"]
        assert snapshot["total_clients"] == 1
        assert snapshot["clients"][0]["last_seen"] == snapshot["status"]["last_success"]
        store.checkpoint()
        assert not Path(path + "-wal").exists() or Path(path + "-wal").stat().st_size == 0
        with sqlite3.connect(path) as reader:
            assert reader.execute("SELECT COUNT(*) FROM aliases").fetchone()[0] == 1
    finally:
        store.close()


def test_version_one_with_weakened_column_definition_is_rejected(tmp_path):
    import sqlite3

    path = str(tmp_path / "wrong-definition.sqlite3")
    original = Store(path)
    original.close()
    with sqlite3.connect(path) as db:
        db.executescript("DROP TABLE devices; CREATE TABLE devices (id TEXT PRIMARY KEY, label TEXT);")
    with pytest.raises(ValueError, match="column definition"):
        Store(path)


def test_network_only_lists_latest_discovered_ssids_and_preserves_history(store, time_at):
    store.ingest([client()], [ap()], inventory(), time_at(0))
    campaign_id = store.create_campaign("Rotation", "Old Wi-Fi", "Future Wi-Fi", time_at(1))
    before = store.network()
    store.failure("Controller unreachable")
    assert store.network()["ssids"] == before["ssids"]
    # An invalid collection rolls back every inventory change.
    with pytest.raises(KeyError):
        store.ingest([], [ap()], [{"ssid": "New Wi-Fi"}, {}], time_at(2))
    assert store.network()["ssids"] == before["ssids"]
    store.ingest([client(ssid="New Wi-Fi")], [ap()], [{"ssid": "New Wi-Fi"}], time_at(3))
    assert [ssid["ssid"] for ssid in store.network()["ssids"]] == ["New Wi-Fi"]
    assert store.network()["ssids"][0]["clients"] == 1
    assert {row["ssid"] for row in store.clients()[0]["sightings"]} == {"Old Wi-Fi", "New Wi-Fi"}
    saved = next(row for row in store.campaigns() if row["id"] == campaign_id)
    assert saved["counts"]["confirmed"] == 1
    store.ingest([], [ap()], [], time_at(4))
    assert store.network()["ssids"] == []
    assert len(store.clients()[0]["sightings"]) == 2
    # Rediscovery uses the exact same SSID identity, including case and spaces.
    store.ingest([], [ap()], [{"ssid": "Old Wi-Fi"}, {"ssid": "old wi-fi "}], time_at(5))
    rediscovered = store.network()["ssids"]
    original = next(row for row in before["ssids"] if row["ssid"] == "Old Wi-Fi")
    assert rediscovered[0]["id"] == original["id"]
    assert rediscovered[1]["id"] != original["id"]
