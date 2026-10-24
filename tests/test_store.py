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


def test_network_retains_absent_ssids_and_preserves_history(store, time_at):
    store.ingest([client()], [ap()], inventory(), time_at(0))
    campaign_id = store.create_campaign("Rotation", "Old Wi-Fi", "Future Wi-Fi", time_at(1))
    before = store.network()
    store.failure("Controller unreachable")
    failed = store.network()["ssids"]
    assert [row["id"] for row in failed] == [row["id"] for row in before["ssids"]]
    assert all(row["connection"] == "unknown" and not row["delete_allowed"] for row in failed)
    # An invalid collection rolls back every inventory change.
    with pytest.raises(KeyError):
        store.ingest([], [ap()], [{"ssid": "New Wi-Fi"}, {}], time_at(2))
    assert store.network()["ssids"] == failed
    store.ingest([client(ssid="New Wi-Fi")], [ap()], [{"ssid": "New Wi-Fi"}], time_at(3))
    retained = {row["ssid"]: row for row in store.network()["ssids"]}
    assert set(retained) == {"Empty guest", "New Wi-Fi", "Old Wi-Fi"}
    assert retained["New Wi-Fi"]["clients"] == 1
    assert retained["New Wi-Fi"]["connection"] == "connected"
    assert not retained["Old Wi-Fi"]["in_inventory"]
    assert retained["Old Wi-Fi"]["last_seen"] == time_at(0)
    assert retained["Old Wi-Fi"]["connection"] == "offline"
    assert retained["Old Wi-Fi"]["delete_allowed"]
    assert {row["ssid"] for row in store.clients()[0]["sightings"]} == {"Old Wi-Fi", "New Wi-Fi"}
    saved = next(row for row in store.campaigns() if row["id"] == campaign_id)
    assert saved["counts"]["confirmed"] == 1
    store.ingest([], [ap()], [], time_at(4))
    assert all(not row["in_inventory"] for row in store.network()["ssids"])
    assert len(store.clients()[0]["sightings"]) == 2
    # Rediscovery uses the exact same SSID identity, including case and spaces.
    store.ingest([], [ap()], [{"ssid": "Old Wi-Fi"}, {"ssid": "old wi-fi "}], time_at(5))
    rediscovered = {row["ssid"]: row for row in store.network()["ssids"]}
    original = next(row for row in before["ssids"] if row["ssid"] == "Old Wi-Fi")
    assert rediscovered["Old Wi-Fi"]["id"] == original["id"]
    assert rediscovered["Old Wi-Fi"]["in_inventory"]
    assert rediscovered["old wi-fi "]["id"] != original["id"]


def test_manual_client_deletion_requires_successful_absence_and_clears_its_history(store, time_at):
    store.ingest([client(), client(mac="aa:bb:cc:00:00:02")], [ap()], inventory(), time_at(0))
    deleted, retained = sorted(store.clients(), key=lambda row: row["mac"])
    store.label(retained["id"], "Retained label")
    store.protect_client(deleted["id"], True)
    assert not deleted["delete_allowed"]
    with pytest.raises(ValueError, match="still detected"):
        store.delete_client(deleted["id"])
    store.ingest([client(mac=retained["mac"])], [ap()], inventory(), time_at(1))
    absent = next(row for row in store.clients() if row["id"] == deleted["id"])
    assert absent["delete_allowed"] and absent["protected"]
    store.delete_client(deleted["id"])
    assert [row["id"] for row in store.clients()] == [retained["id"]]
    assert store.clients()[0]["label"] == "Retained label"
    for table in ("aliases", "sightings"):
        assert (
            store.db.execute(f"SELECT COUNT(*) FROM {table} WHERE mac=?", (deleted["mac"],)).fetchone()[0]
            == 0
        )
    assert deleted["mac"] not in store._protected_macs()
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(ValueError, match="not found"):
        store.delete_client(deleted["id"])


@pytest.mark.parametrize("failure", [False, True])
def test_failed_or_stale_collection_blocks_deletion_and_cleanup(store, time_at, failure):
    store.ingest([client()], [ap()], inventory(), time_at(-60 * 86400))
    device = store.clients()[0]["id"]
    store.ingest([], [], [], time_at(0) if failure else time_at(-91))
    if failure:
        store.failure("Controller unavailable")
    assert not store.clients()[0]["delete_allowed"]
    assert all(not row["delete_allowed"] for row in store.network()["ssids"])
    with pytest.raises(ValueError, match="fresh successful collection"):
        store.delete_client(device)
    with pytest.raises(ValueError, match="fresh successful collection"):
        store.delete_ssid("Old Wi-Fi")
    assert store.cleanup(30, 30, at=time_at(0)) == {"clients": 0, "ssids": 0}
    assert len(store.clients()) == 1 and len(store.network()["ssids"]) == 3


def test_startup_blocks_deletion_until_new_success_even_with_recent_saved_state(tmp_path, time_at):
    path = str(tmp_path / "startup.sqlite3")
    first = Store(path)
    first.ingest([client()], [], inventory(), time_at(-60 * 86400))
    device = first.clients()[0]["id"]
    first.ingest([], [], [], time_at(0))
    first.close()
    second = Store(path)
    try:
        assert second.status()["fresh"]
        assert not second.clients()[0]["delete_allowed"]
        with pytest.raises(ValueError, match="fresh successful collection"):
            second.delete_client(device)
        with pytest.raises(ValueError, match="fresh successful collection"):
            second.delete_ssid("Old Wi-Fi")
        assert second.cleanup(30, 30, at=time_at(1)) == {"clients": 0, "ssids": 0}
        second.ingest([], [], [], time_at(1))
        assert second.cleanup(30, 30, at=time_at(1)) == {"clients": 1, "ssids": 3}
    finally:
        second.close()


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("evidence_only", [False, True])
def test_saved_campaign_members_and_evidence_prevent_client_deletion(
    store, time_at, completed, evidence_only
):
    store.ingest([client()], [], inventory(), time_at(-60 * 86400))
    cid = store.create_campaign("Keep history", "Old Wi-Fi", "New Wi-Fi", time_at(-50 * 86400))
    store.ingest([client(ssid="New Wi-Fi")], [], inventory(), time_at(-40 * 86400))
    if completed:
        with store.db:
            store.db.execute("UPDATE campaigns SET completed_at=? WHERE id=?", (time_at(-39 * 86400), cid))
    if evidence_only:
        with store.db:
            store.db.execute("DELETE FROM members WHERE campaign_id=?", (cid,))
    device = store.clients()[0]["id"]
    store.ingest([], [], [], time_at(0))
    assert "saved migration campaign" in store.clients()[0]["delete_reason"]
    with pytest.raises(ValueError, match="saved migration campaign"):
        store.delete_client(device)
    assert store.cleanup(30, None, at=time_at(0)) == {"clients": 0, "ssids": 0}
    store.delete_campaign(cid)
    assert store.clients()[0]["delete_allowed"]
    store.delete_client(device)
    assert not store.clients()


def test_linked_client_deletion_requires_every_alias_absent(store, time_at):
    other = client(mac="aa:bb:cc:00:00:02")
    store.ingest([client(), other], [], [], time_at(0))
    device = next(row for row in store.clients() if row["mac"] != other["mac"])["id"]
    store.link(device, other["mac"])
    store.ingest([other], [], [], time_at(1))
    assert not store.clients()[0]["delete_allowed"]
    with pytest.raises(ValueError, match="still detected"):
        store.delete_client(device)
    store.ingest([], [], [], time_at(2))
    store.delete_client(device)
    assert not store.clients()
    assert store.db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0


def test_client_protection_follows_macs_across_link_unlink_and_restart(tmp_path, time_at):
    path = str(tmp_path / "protection.sqlite3")
    first = Store(path)
    other = client(mac="aa:bb:cc:00:00:02")
    first.ingest([client(), other], [], [], time_at(-60 * 86400))
    devices = {row["mac"]: row["id"] for row in first.clients()}
    first.protect_client(devices[client()["mac"]], True)
    first.link(devices[other["mac"]], client()["mac"])
    assert first.clients()[0]["protected"]
    new_id = first.unlink(devices[other["mac"]], client()["mac"])
    assert {row["id"]: row["protected"] for row in first.clients()} == {
        devices[other["mac"]]: False,
        new_id: True,
    }
    first.link(devices[other["mac"]], client()["mac"])
    first.protect_client(devices[other["mac"]], True)
    new_id = first.unlink(devices[other["mac"]], client()["mac"])
    assert all(row["protected"] for row in first.clients())
    first.protect_client(new_id, False)
    first.close()
    second = Store(path)
    try:
        second.ingest([], [], [], time_at(0))
        assert second.cleanup(30, None, at=time_at(0)) == {"clients": 1, "ssids": 0}
        assert second.clients()[0]["id"] == devices[other["mac"]]
        assert second.clients()[0]["protected"]
        second.protect_client(devices[other["mac"]], False)
        assert second.cleanup(30, None, at=time_at(0)) == {"clients": 1, "ssids": 0}
    finally:
        second.close()


def test_protect_and_unprotect_apply_to_all_current_aliases(store, time_at):
    store.ingest([client(), client(mac="aa:bb:cc:00:00:02")], [], [], time_at(0))
    device = store.clients()[0]["id"]
    other = next(row for row in store.clients() if row["id"] != device)["mac"]
    store.protect_client(device, True)
    store.link(device, other)
    store.protect_client(device, False)
    assert not store._protected_macs()
    store.protect_client(device, True)
    assert store._protected_macs() == {client()["mac"], "aa:bb:cc:00:00:02"}
    with pytest.raises(ValueError, match="true or false"):
        store.protect_client(device, 1)
    with pytest.raises(ValueError, match="not found"):
        store.protect_client("missing", True)


@pytest.mark.parametrize("elapsed", [30 * 86400 - 0.000001, 30 * 86400, 30 * 86400 + 1])
def test_cleanup_retention_boundary_and_disabled_categories(store, time_at, elapsed):
    store.ingest([client()], [], [{"ssid": "Old Wi-Fi"}], time_at(-elapsed))
    store.ingest([], [], [], time_at(0))
    assert store.cleanup(None, None, at=time_at(0)) == {"clients": 0, "ssids": 0}
    count = int(elapsed >= 30 * 86400)
    assert store.cleanup(30, 30, at=time_at(0)) == {"clients": count, "ssids": count}
    assert len(store.clients()) == 1 - count
    assert len(store.network()["ssids"]) == 1 - count


@pytest.mark.parametrize("days", [0, -1, True, False, 1.5, "30"])
def test_cleanup_rejects_invalid_retention_days(store, days):
    for values in ((days, None), (None, days)):
        with pytest.raises(ValueError, match="positive integer"):
            store.cleanup(*values)


def test_cleanup_handles_large_days_and_uses_latest_alias_seen(store, time_at):
    other = client(mac="aa:bb:cc:00:00:02")
    store.ingest([client()], [], [], time_at(-60 * 86400))
    device = store.clients()[0]["id"]
    store.ingest([other], [], [], time_at(-10 * 86400))
    store.link(device, other["mac"])
    store.ingest([], [], [], time_at(0))
    assert store.cleanup(10**100, 10**100, at=time_at(0)) == {"clients": 0, "ssids": 0}
    assert store.cleanup(30, None, at=time_at(0)) == {"clients": 0, "ssids": 0}


def test_cleanup_skips_current_clients_and_inventory(store, time_at):
    store.ingest([client()], [], [{"ssid": "Old Wi-Fi"}], time_at(-60 * 86400))
    store.ingest([client()], [], [{"ssid": "Old Wi-Fi"}], time_at(0))
    assert store.cleanup(1, 1, at=time_at(0)) == {"clients": 0, "ssids": 0}


def test_cleanup_enabled_tracker_protects_every_identity_alias_until_disabled(store, time_at):
    first, second = client(), client(mac="aa:bb:cc:00:00:02")
    store.ingest([first, second], [], [], time_at(-60 * 86400))
    device = store.clients()[0]["id"]
    store.link(device, next(row["mac"] for row in store.clients() if row["id"] != device))
    store.ingest([], [], [], time_at(0))
    assert store.cleanup(30, None, time_at(0), {second["mac"]}) == {"clients": 0, "ssids": 0}
    assert len(store.clients()[0]["macs"]) == 2
    # Registry exemption is transient and does not become explicit protection.
    assert not store.clients()[0]["protected"]
    assert store.cleanup(30, None, time_at(0), set()) == {"clients": 1, "ssids": 0}
    assert not store.clients()


def test_enabled_tracker_does_not_block_explicit_manual_deletion(store, time_at):
    store.ingest([client()], [], [], time_at(-60 * 86400))
    device = store.clients()[0]["id"]
    store.ingest([], [], [], time_at(0))
    assert store.cleanup(30, None, time_at(0), {client()["mac"]}) == {"clients": 0, "ssids": 0}
    store.delete_client(device)
    assert not store.clients()


def test_ssid_deletion_is_exact_and_preserves_campaigns_and_sightings(store, time_at):
    names = ["Old Wi-Fi", "old wi-fi", "Old Wi-Fi "]
    store.ingest([client()], [ap()], [{"ssid": name} for name in names], time_at(0))
    store.create_campaign("Retain", names[0], names[1], time_at(1))
    store.ingest([], [ap()], [], time_at(2))
    before_campaign = store.campaigns()
    before_clients = store.clients()
    store.delete_ssid(names[0])
    assert {row["ssid"] for row in store.network()["ssids"]} == set(names[1:])
    assert store.clients() == before_clients
    assert store.campaigns() == before_campaign
    assert store.network()["aps"][0]["online"]
    with pytest.raises(ValueError, match="SSID not found"):
        store.delete_ssid(names[0])
    store.ingest([], [], [{"ssid": names[0]}], time_at(3))
    with pytest.raises(ValueError, match="still reported"):
        store.delete_ssid(names[0])


def test_current_client_prevents_ssid_deletion_without_inventory(store, time_at):
    store.ingest([], [], [{"ssid": "Old Wi-Fi"}], time_at(-60 * 86400))
    store.ingest([client()], [], [], time_at(0))
    ssid = store.network()["ssids"][0]
    assert not ssid["in_inventory"]
    assert ssid["connection"] == "connected"
    assert ssid["last_seen"] == time_at(0)
    assert not ssid["delete_allowed"]
    with pytest.raises(ValueError, match="Clients are still detected"):
        store.delete_ssid("Old Wi-Fi")
    assert store.cleanup(None, 30, at=time_at(0)) == {"clients": 0, "ssids": 0}


def test_simultaneous_sightings_on_one_mac_prevent_deleting_either_ssid(store, time_at):
    store.ingest([], [], [{"ssid": "Old Wi-Fi"}, {"ssid": "New Wi-Fi"}], time_at(-60 * 86400))
    store.ingest([client(), client(ssid="New Wi-Fi")], [], [], time_at(0))
    for name in ("Old Wi-Fi", "New Wi-Fi"):
        with pytest.raises(ValueError, match="Clients are still detected"):
            store.delete_ssid(name)
    assert all(row["connection"] == "connected" for row in store.network()["ssids"])
    assert store.cleanup(None, 30, at=time_at(0)) == {"clients": 0, "ssids": 0}


def test_client_deletion_preserves_ssid_last_seen_for_longer_retention(store, time_at):
    store.ingest([], [], [{"ssid": "Old Wi-Fi"}], time_at(-90 * 86400))
    store.ingest([client()], [], [], time_at(-30 * 86400))
    store.ingest([], [], [], time_at(0))
    assert store.cleanup(30, 60, at=time_at(0)) == {"clients": 1, "ssids": 0}
    assert store.network()["ssids"][0]["last_seen"] == time_at(-30 * 86400)
    store.ingest([], [], [], time_at(30 * 86400))
    assert store.cleanup(30, 60, at=time_at(30 * 86400)) == {"clients": 0, "ssids": 1}
    assert store._get("ssid_last_seen") == {}


@pytest.mark.parametrize("operation", ["client", "ssid", "cleanup"])
def test_inventory_deletion_rolls_back_all_changes_on_storage_error(store, time_at, operation):
    import sqlite3

    store.ingest([client()], [], [{"ssid": "Old Wi-Fi"}], time_at(-60 * 86400))
    device = store.clients()[0]["id"]
    store.protect_client(device, True)
    store.ingest([], [], [], time_at(0))
    table = "inventory" if operation in ("ssid", "cleanup") else "devices"
    if operation == "cleanup":
        store.protect_client(device, False)
    before = {
        table: [tuple(row) for row in store.db.execute(f"SELECT * FROM {table}")]
        for table in ("meta", "devices", "aliases", "sightings", "inventory")
    }
    store.db.executescript(f"""CREATE TEMP TRIGGER abort_inventory_delete BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT, 'test delete failure'); END;""")
    with pytest.raises(sqlite3.IntegrityError, match="test delete failure"):
        if operation == "client":
            store.delete_client(device)
        elif operation == "ssid":
            store.delete_ssid("Old Wi-Fi")
        else:
            store.cleanup(30, 30, at=time_at(0))
    for table, rows in before.items():
        assert [tuple(row) for row in store.db.execute(f"SELECT * FROM {table}")] == rows
