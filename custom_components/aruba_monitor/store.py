from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .const import TESTED_FIRMWARE


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def ssid_id(ssid: str) -> str:
    return hashlib.sha256(ssid.encode()).hexdigest()[:24]


SCHEMA_VERSION = 1
SCHEMA_COLUMNS = {
    "meta": ("key", "value"),
    "devices": ("id", "label"),
    "aliases": ("mac", "device_id", "first_seen", "last_seen", "payload"),
    "sightings": ("mac", "ssid", "first_seen", "last_seen"),
    "inventory": ("kind", "id", "payload", "seen_at"),
    "campaigns": ("id", "name", "source", "target", "created_at", "completed_at"),
    "members": ("campaign_id", "mac", "excluded", "note"),
    "evidence": ("campaign_id", "mac", "ssid", "first_seen", "last_seen"),
}
SCHEMA_PRIMARY_KEYS = {
    "meta": ("key",),
    "devices": ("id",),
    "aliases": ("mac",),
    "sightings": ("mac", "ssid"),
    "inventory": ("kind", "id"),
    "campaigns": ("id",),
    "members": ("campaign_id", "mac"),
    "evidence": ("campaign_id", "mac", "ssid"),
}
SCHEMA_FOREIGN_KEYS = {
    "aliases": {("device_id", "devices", "id")},
    "sightings": {("mac", "aliases", "mac")},
    "members": {("campaign_id", "campaigns", "id"), ("mac", "aliases", "mac")},
    "evidence": {("campaign_id", "campaigns", "id"), ("mac", "aliases", "mac")},
}
SCHEMA_SQL = """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE devices (id TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '');
            CREATE TABLE aliases (
                mac TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX aliases_device ON aliases(device_id);
            CREATE TABLE sightings (
                mac TEXT NOT NULL REFERENCES aliases(mac), ssid TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY(mac, ssid)
            );
            CREATE TABLE inventory (
                kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                seen_at TEXT NOT NULL, PRIMARY KEY(kind, id)
            );
            CREATE TABLE campaigns (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
                created_at TEXT NOT NULL, completed_at TEXT
            );
            CREATE TABLE members (
                campaign_id TEXT NOT NULL REFERENCES campaigns(id), mac TEXT NOT NULL REFERENCES aliases(mac),
                excluded INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', PRIMARY KEY(campaign_id, mac)
            );
            CREATE TABLE evidence (
                campaign_id TEXT NOT NULL REFERENCES campaigns(id), mac TEXT NOT NULL REFERENCES aliases(mac),
                ssid TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                PRIMARY KEY(campaign_id, mac, ssid)
            );
"""


def validate_schema(db: sqlite3.Connection) -> None:
    """Reject unsupported databases before making any persistent changes."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported Aruba Monitor database schema version {version}")
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables != set(SCHEMA_COLUMNS):
        raise ValueError("Aruba Monitor database has unexpected or missing tables")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger', 'view')").fetchone():
        raise ValueError("Aruba Monitor database contains unsupported triggers or views")
    for table, expected in SCHEMA_COLUMNS.items():
        columns = list(db.execute(f'PRAGMA table_info("{table}")'))
        if tuple(row[1] for row in columns) != expected:
            raise ValueError(f"Aruba Monitor database has an unsupported {table} table")
        nullable = {
            ("meta", "key"),
            ("devices", "id"),
            ("aliases", "mac"),
            ("campaigns", "id"),
            ("campaigns", "completed_at"),
        }
        defaults = {("devices", "label"): "''", ("members", "excluded"): "0", ("members", "note"): "''"}
        for column in columns:
            identity = (table, column[1])
            expected_type = "INTEGER" if identity == ("members", "excluded") else "TEXT"
            if (
                column[2].upper() != expected_type
                or column[3] != int(identity not in nullable)
                or column[4] != defaults.get(identity)
            ):
                raise ValueError(f"Unsupported column definition in {table}")
        primary_key = tuple(row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5])
        if primary_key != SCHEMA_PRIMARY_KEYS[table]:
            raise ValueError(f"Aruba Monitor database has an unsupported {table} primary key")
        foreign_keys = {(row[3], row[2], row[4]) for row in db.execute(f'PRAGMA foreign_key_list("{table}")')}
        if any(
            tuple(row[5:]) != ("NO ACTION", "NO ACTION", "NONE")
            for row in db.execute(f'PRAGMA foreign_key_list("{table}")')
        ):
            raise ValueError(f"Unsupported foreign key behavior in {table}")
        if foreign_keys != SCHEMA_FOREIGN_KEYS.get(table, set()):
            raise ValueError(f"Aruba Monitor database has unsupported {table} relationships")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Aruba Monitor database contains invalid relationships")


class Store:
    def __init__(self, path: str, interval: float = 30, *, require_live_collection: bool = False):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.interval = interval
        self.require_live_collection = require_live_collection
        self._has_live_collection = False
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            objects = self.db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()
            if version == 0 and objects is None:
                self.db.executescript(f"BEGIN;{SCHEMA_SQL}PRAGMA user_version={SCHEMA_VERSION};COMMIT;")
            validate_schema(self.db)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
        except Exception:
            self.db.close()
            raise

    def _get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, key, value):
        self.db.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def set_meta(self, key, value):
        with self.lock, self.db:
            self._set(key, value)

    def failure(self, message):
        with self.lock, self.db:
            self._set("last_error", message)
            self._set("last_attempt", utcnow())

    def status(self, now: str | None = None) -> dict:
        with self.lock:
            last = self._get("last_success")
            age = (
                max(
                    0,
                    (datetime.fromisoformat(now or utcnow()) - datetime.fromisoformat(last)).total_seconds(),
                )
                if last
                else None
            )
            awaiting_collection = self.require_live_collection and not self._has_live_collection
            stale = awaiting_collection or age is None or age >= 3 * self.interval
            error = self._get("last_error")
            return {
                "live_collection": self._has_live_collection,
                "last_success": last,
                "last_attempt": self._get("last_attempt"),
                "last_error": error,
                "age_seconds": age,
                "stale": stale,
                "fresh": not stale and not error,
                "poll_interval": self.interval,
                "firmware": self._get("firmware"),
                "firmware_verified": self._get("firmware") in TESTED_FIRMWARE,
            }

    def ingest(self, clients: list[dict], aps: list[dict], networks: list[dict], at: str | None = None):
        at = at or utcnow()
        with self.lock:
            with self.db:
                active = list(self.db.execute("SELECT * FROM campaigns WHERE completed_at IS NULL"))
                for client in clients:
                    mac = client["mac"]
                    if not self.db.execute("SELECT 1 FROM aliases WHERE mac=?", (mac,)).fetchone():
                        device = uuid.uuid4().hex
                        self.db.execute("INSERT INTO devices(id) VALUES (?)", (device,))
                        self.db.execute(
                            "INSERT INTO aliases VALUES (?,?,?,?,?)",
                            (mac, device, at, at, json.dumps(client)),
                        )
                    else:
                        self.db.execute(
                            "UPDATE aliases SET last_seen=?, payload=? WHERE mac=?",
                            (at, json.dumps(client), mac),
                        )
                    self.db.execute(
                        """INSERT INTO sightings VALUES (?,?,?,?) ON CONFLICT(mac,ssid)
                                       DO UPDATE SET last_seen=excluded.last_seen""",
                        (mac, client["ssid"], at, at),
                    )
                    for campaign in active:
                        if client["ssid"] == campaign["source"]:
                            self.db.execute(
                                "INSERT OR IGNORE INTO members(campaign_id,mac) VALUES (?,?)",
                                (campaign["id"], mac),
                            )
                        # Keep all SSIDs, including aliases not yet enrolled or linked.
                        # Older databases already support this evidence format; completed
                        # campaigns retain their frozen source/target evidence unchanged.
                        if at >= campaign["created_at"]:
                            self.db.execute(
                                """INSERT INTO evidence VALUES (?,?,?,?,?)
                                ON CONFLICT(campaign_id,mac,ssid) DO UPDATE SET last_seen=excluded.last_seen""",
                                (campaign["id"], mac, client["ssid"], at, at),
                            )
                for kind, items in (("ap", aps), ("ssid", networks)):
                    for item in items:
                        key = item["id"] if kind == "ap" else ssid_id(item["ssid"])
                        self.db.execute(
                            """INSERT INTO inventory VALUES (?,?,?,?) ON CONFLICT(kind,id)
                            DO UPDATE SET payload=excluded.payload, seen_at=excluded.seen_at""",
                            (kind, key, json.dumps(item), at),
                        )
                self._set("last_success", at)
                self._set("last_attempt", at)
                self._set("last_error", None)
            self._has_live_collection = True

    def _clients(self):
        status = self.status()
        protected_macs = self._protected_macs()
        result = []
        for device in self.db.execute("SELECT * FROM devices ORDER BY id"):
            aliases = list(
                self.db.execute("SELECT * FROM aliases WHERE device_id=? ORDER BY mac", (device["id"],))
            )
            if not aliases:
                continue
            latest = max(aliases, key=lambda alias: alias["last_seen"])
            payload = json.loads(latest["payload"])
            observations = [
                dict(row)
                for row in self.db.execute(
                    """SELECT s.* FROM sightings s
                JOIN aliases a ON s.mac=a.mac WHERE a.device_id=? ORDER BY s.last_seen DESC""",
                    (device["id"],),
                )
            ]
            connected = any(alias["last_seen"] == status["last_success"] for alias in aliases)
            delete_reason = self._client_delete_reason(device["id"], aliases, status)
            result.append(
                {
                    **payload,
                    "id": device["id"],
                    "label": device["label"],
                    "macs": [alias["mac"] for alias in aliases],
                    "first_seen": min(alias["first_seen"] for alias in aliases),
                    "last_seen": latest["last_seen"],
                    "sightings": observations,
                    "connection": ("connected" if connected else "offline") if status["fresh"] else "unknown",
                    "protected": any(alias["mac"] in protected_macs for alias in aliases),
                    "delete_allowed": delete_reason is None,
                    "delete_reason": delete_reason,
                }
            )
        return result

    def clients(self):
        with self.lock:
            return self._clients()

    def _client_connections(self, status: dict) -> list[dict]:
        """Keep each MAC's observations separate from campaign identity grouping."""
        return [
            {
                **json.loads(row["payload"]),
                "mac": row["mac"],
                "label": row["label"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "connection": ("connected" if row["last_seen"] == status["last_success"] else "offline")
                if status["fresh"]
                else "unknown",
            }
            for row in self.db.execute(
                """SELECT a.*,d.label FROM aliases a JOIN devices d ON a.device_id=d.id
                   ORDER BY a.mac"""
            )
        ]

    def network(self):
        with self.lock:
            status = self.status()
            last = status["last_success"]
            current = [
                json.loads(row[0])
                for row in self.db.execute("SELECT payload FROM aliases WHERE last_seen=?", (last,))
            ]
            aps = []
            for row in self.db.execute("SELECT * FROM inventory WHERE kind='ap' ORDER BY id"):
                aps.append(
                    {
                        **json.loads(row["payload"]),
                        "online": row["seen_at"] == last,
                        "last_seen": row["seen_at"],
                        "stale": status["stale"],
                    }
                )
            ssids = []
            retained = [
                (json.loads(row["payload"])["ssid"], row)
                for row in self.db.execute("SELECT * FROM inventory WHERE kind='ssid'")
            ]
            for name, row in sorted(retained, key=lambda item: item[0]):
                clients = [client for client in current if client["ssid"] == name]
                by_ap, by_band = {}, {}
                for client in clients:
                    by_ap[client["ap"]] = by_ap.get(client["ap"], 0) + 1
                    band = client.get("band") or "unknown"
                    by_band[band] = by_band.get(band, 0) + 1
                in_inventory = row["seen_at"] == last
                detected = self._ssid_detected(name, last)
                delete_reason = self._ssid_delete_reason(row, status)
                ssids.append(
                    {
                        "id": ssid_id(name),
                        "ssid": name,
                        "clients": len(clients),
                        "by_ap": by_ap,
                        "by_band": by_band,
                        "in_inventory": in_inventory,
                        "last_seen": self._ssid_last_seen(name, row["seen_at"]),
                        "connection": ("connected" if in_inventory or detected else "offline")
                        if status["fresh"]
                        else "unknown",
                        "delete_allowed": delete_reason is None,
                        "delete_reason": delete_reason,
                        "stale": status["stale"],
                    }
                )
            return {"aps": aps, "ssids": ssids, "total_clients": len(current)}

    def _protected_macs(self) -> set[str]:
        """Store protection on original MACs so identity corrections preserve it."""
        return set(self._get("protected_client_macs", []))

    @staticmethod
    def _collection_delete_reason(status: dict) -> str | None:
        if not status["fresh"] or not status["live_collection"]:
            return "A fresh successful collection is required before deletion."
        return None

    def _client_delete_reason(self, device_id: str, aliases: list, status: dict) -> str | None:
        if reason := self._collection_delete_reason(status):
            return reason
        if any(alias["last_seen"] == status["last_success"] for alias in aliases):
            return "This client is still detected by the controller."
        if self.db.execute(
            """SELECT 1 FROM aliases a WHERE a.device_id=? AND (
                EXISTS (SELECT 1 FROM members m WHERE m.mac=a.mac) OR
                EXISTS (SELECT 1 FROM evidence e WHERE e.mac=a.mac)) LIMIT 1""",
            (device_id,),
        ).fetchone():
            return "This client is retained by a saved migration campaign."
        return None

    def _ssid_detected(self, name: str, last_success: str | None) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM sightings WHERE ssid=? AND last_seen=? LIMIT 1", (name, last_success)
            ).fetchone()
            is not None
        )

    def _ssid_delete_reason(self, row, status: dict) -> str | None:
        if reason := self._collection_delete_reason(status):
            return reason
        if row["seen_at"] == status["last_success"]:
            return "This SSID is still reported by the controller."
        if self._ssid_detected(json.loads(row["payload"])["ssid"], status["last_success"]):
            return "Clients are still detected on this SSID."
        return None

    def _ssid_last_seen(self, name: str, inventory_seen: str) -> str:
        sighting = self.db.execute("SELECT MAX(last_seen) FROM sightings WHERE ssid=?", (name,)).fetchone()[0]
        retained = self._get("ssid_last_seen", {}).get(ssid_id(name))
        return max(value for value in (inventory_seen, sighting, retained) if value)

    def protect_client(self, device_id: str, protected: bool):
        """Change automatic cleanup protection for every MAC in this identity."""
        if not isinstance(protected, bool):
            raise ValueError("Client protection must be true or false")
        with self.lock, self.db:
            self._device(device_id)
            macs = {
                row[0] for row in self.db.execute("SELECT mac FROM aliases WHERE device_id=?", (device_id,))
            }
            protected_macs = self._protected_macs()
            if protected:
                protected_macs.update(macs)
            else:
                protected_macs.difference_update(macs)
            self._set("protected_client_macs", sorted(protected_macs))

    def _delete_client(self, device_id: str, aliases: list):
        macs = {alias["mac"] for alias in aliases}
        # Client history can expire earlier than SSID inventory. Preserve its last
        # observation so removing that history cannot shorten the SSID's lifetime.
        ssid_seen = self._get("ssid_last_seen", {})
        for row in self.db.execute(
            """SELECT s.ssid,MAX(s.last_seen) AS last_seen FROM sightings s
            JOIN aliases a ON a.mac=s.mac WHERE a.device_id=? GROUP BY s.ssid""",
            (device_id,),
        ):
            key = ssid_id(row["ssid"])
            if self.db.execute("SELECT 1 FROM inventory WHERE kind='ssid' AND id=?", (key,)).fetchone():
                ssid_seen[key] = max(ssid_seen.get(key, row["last_seen"]), row["last_seen"])
        self._set("ssid_last_seen", ssid_seen)
        self.db.execute(
            "DELETE FROM sightings WHERE mac IN (SELECT mac FROM aliases WHERE device_id=?)", (device_id,)
        )
        self.db.execute("DELETE FROM aliases WHERE device_id=?", (device_id,))
        self.db.execute("DELETE FROM devices WHERE id=?", (device_id,))
        self._set("protected_client_macs", sorted(self._protected_macs() - macs))

    def delete_client(self, device_id: str):
        """Forget an absent identity and its sightings without changing campaigns."""
        with self.lock, self.db:
            self._device(device_id)
            aliases = list(self.db.execute("SELECT * FROM aliases WHERE device_id=?", (device_id,)))
            if reason := self._client_delete_reason(device_id, aliases, self.status()):
                raise ValueError(reason)
            self._delete_client(device_id, aliases)

    def delete_ssid(self, ssid: str):
        """Forget only absent SSID inventory; historical sightings remain intact."""
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM inventory WHERE kind='ssid' AND id=?", (ssid_id(ssid),)
            ).fetchone()
            if not row:
                raise ValueError("SSID not found")
            status = self.status()
            if reason := self._ssid_delete_reason(row, status):
                raise ValueError(reason)
            self._delete_ssid(row["id"])

    def _delete_ssid(self, identity: str):
        self.db.execute("DELETE FROM inventory WHERE kind='ssid' AND id=?", (identity,))
        ssid_seen = self._get("ssid_last_seen", {})
        ssid_seen.pop(identity, None)
        self._set("ssid_last_seen", ssid_seen)

    def cleanup(
        self, client_retention_days: int | None, ssid_retention_days: int | None, at: str | None = None
    ) -> dict[str, int]:
        """Expire absent inventory after a successful poll, preserving campaign history."""
        for days in (client_retention_days, ssid_retention_days):
            if days is not None and (type(days) is not int or days < 1):
                raise ValueError("Retention days must be a positive integer or disabled")
        at = at or utcnow()
        now = datetime.fromisoformat(at)
        removed = {"clients": 0, "ssids": 0}
        with self.lock, self.db:
            status = self.status(now=at)
            if self._collection_delete_reason(status):
                return removed
            if client_retention_days is not None:
                protected = self._protected_macs()
                for device in list(self.db.execute("SELECT id FROM devices")):
                    aliases = list(
                        self.db.execute("SELECT * FROM aliases WHERE device_id=?", (device["id"],))
                    )
                    if not aliases or any(alias["mac"] in protected for alias in aliases):
                        continue
                    if self._client_delete_reason(device["id"], aliases, status):
                        continue
                    last_seen = max(datetime.fromisoformat(alias["last_seen"]) for alias in aliases)
                    if (now - last_seen).total_seconds() / 86400 >= client_retention_days:
                        self._delete_client(device["id"], aliases)
                        removed["clients"] += 1
            if ssid_retention_days is not None:
                for row in list(self.db.execute("SELECT * FROM inventory WHERE kind='ssid'")):
                    name = json.loads(row["payload"])["ssid"]
                    if self._ssid_delete_reason(row, status):
                        continue
                    last_seen = datetime.fromisoformat(self._ssid_last_seen(name, row["seen_at"]))
                    if (now - last_seen).total_seconds() / 86400 >= ssid_retention_days:
                        self._delete_ssid(row["id"])
                        removed["ssids"] += 1
            return removed

    def _device(self, device_id):
        if not self.db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
            raise ValueError("Device not found")

    def label(self, device_id: str, label: str):
        with self.lock, self.db:
            self._device(device_id)
            self.db.execute("UPDATE devices SET label=? WHERE id=?", (label, device_id))

    def link(self, device_id: str, mac: str):
        with self.lock, self.db:
            self._device(device_id)
            previous = self.db.execute("SELECT device_id FROM aliases WHERE mac=?", (mac,)).fetchone()
            if not previous:
                raise ValueError("MAC must have been observed before it can be linked")
            self.db.execute("UPDATE aliases SET device_id=? WHERE mac=?", (device_id, mac))
            self.db.execute(
                "DELETE FROM devices WHERE id=? AND NOT EXISTS (SELECT 1 FROM aliases WHERE device_id=?)",
                (previous[0], previous[0]),
            )

    def unlink(self, device_id: str, mac: str):
        with self.lock, self.db:
            self._device(device_id)
            aliases = list(self.db.execute("SELECT mac FROM aliases WHERE device_id=?", (device_id,)))
            if mac not in [row[0] for row in aliases]:
                raise ValueError("MAC does not belong to this device")
            if len(aliases) < 2:
                raise ValueError("Cannot unlink a device's only MAC")
            new_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO devices(id) VALUES (?)", (new_id,))
            self.db.execute("UPDATE aliases SET device_id=? WHERE mac=?", (new_id, mac))
            return new_id

    def create_campaign(self, name, source, target, at: str | None = None):
        if source == target:
            raise ValueError("Source and target SSIDs must differ")
        with self.lock, self.db:
            campaign_id = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO campaigns VALUES (?,?,?,?,?,NULL)",
                (campaign_id, name, source, target, at or utcnow()),
            )
            self.db.execute(
                """INSERT INTO members(campaign_id,mac)
                               SELECT ?,mac FROM sightings WHERE ssid=?""",
                (campaign_id, source),
            )
            return campaign_id

    def _campaign(self, campaign_id):
        row = self.db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row:
            raise ValueError("Campaign not found")
        return row

    def delete_campaign(self, campaign_id: str):
        with self.lock, self.db:
            self._campaign(campaign_id)
            self.db.execute("DELETE FROM evidence WHERE campaign_id=?", (campaign_id,))
            self.db.execute("DELETE FROM members WHERE campaign_id=?", (campaign_id,))
            self.db.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))

    def campaigns(self):
        with self.lock:
            clients = {client["id"]: client for client in self._clients()}
            result = []
            for campaign in self.db.execute("SELECT * FROM campaigns ORDER BY created_at DESC"):
                groups = {}
                members = self.db.execute(
                    """SELECT m.*,a.device_id FROM members m JOIN aliases a ON m.mac=a.mac
                                             WHERE campaign_id=?""",
                    (campaign["id"],),
                )
                for member in members:
                    groups.setdefault(member["device_id"], []).append(member)
                rows, counts = [], {"pending": 0, "confirmed": 0, "returned": 0, "excluded": 0}
                for device_id, members in groups.items():
                    evidence = list(
                        self.db.execute(
                            """SELECT e.* FROM evidence e JOIN aliases a ON e.mac=a.mac
                        WHERE e.campaign_id=? AND a.device_id=?""",
                            (campaign["id"], device_id),
                        )
                    )
                    targets = [row["last_seen"] for row in evidence if row["ssid"] == campaign["target"]]
                    away = [row for row in evidence if row["ssid"] != campaign["source"]]
                    last_away = max((row["last_seen"] for row in away), default=None)
                    sources = [row["last_seen"] for row in evidence if row["ssid"] == campaign["source"]]
                    first_target = min(
                        (row["first_seen"] for row in evidence if row["ssid"] == campaign["target"]),
                        default=None,
                    )
                    state = "pending"
                    if last_away:
                        state = "returned" if sources and max(sources) >= last_away else "confirmed"
                    excluded = any(member["excluded"] for member in members)
                    if excluded:
                        state = "excluded"
                    counts[state] += 1
                    rows.append(
                        {
                            **clients[device_id],
                            "migration_state": state,
                            "first_target_seen": first_target,
                            "last_target_seen": max(targets, default=None),
                            "first_away_seen": min((row["first_seen"] for row in away), default=None),
                            "last_away_seen": last_away,
                            "exclusion_note": "; ".join(
                                dict.fromkeys(m["note"] for m in members if m["note"])
                            ),
                        }
                    )
                eligible = counts["pending"] + counts["returned"] + counts["confirmed"]
                result.append(
                    {
                        **dict(campaign),
                        "counts": counts,
                        "total": len(rows),
                        "eligible": eligible,
                        "progress_pct": round(100 * counts["confirmed"] / eligible, 1) if eligible else 0,
                        "ready": bool(
                            eligible
                            and not counts["pending"]
                            and not counts["returned"]
                            and self.status()["fresh"]
                        ),
                        "clients": rows,
                    }
                )
            return result

    def exclude(self, campaign_id, device_id, excluded, note):
        with self.lock, self.db:
            campaign = self._campaign(campaign_id)
            if campaign["completed_at"]:
                raise ValueError("Completed campaign exclusions are read-only")
            macs = [
                row[0]
                for row in self.db.execute(
                    """SELECT m.mac FROM members m JOIN aliases a ON a.mac=m.mac
                WHERE m.campaign_id=? AND a.device_id=?""",
                    (campaign_id, device_id),
                )
            ]
            if not macs:
                raise ValueError("Device is not enrolled in this campaign")
            for mac in macs:
                self.db.execute(
                    "UPDATE members SET excluded=?, note=? WHERE campaign_id=? AND mac=?",
                    (int(excluded), note if excluded else "", campaign_id, mac),
                )

    def complete(self, campaign_id):
        with self.lock, self.db:
            campaign = self._campaign(campaign_id)
            if campaign["completed_at"]:
                return
            detail = next(c for c in self.campaigns() if c["id"] == campaign_id)
            if not detail["ready"]:
                raise ValueError("Campaign needs fresh collection and all included devices confirmed")
            self.db.execute("UPDATE campaigns SET completed_at=? WHERE id=?", (utcnow(), campaign_id))

    def snapshot(self) -> dict:
        """Return one internally consistent view for entities and the sidebar."""
        with self.lock:
            status = self.status()
            return {
                "status": status,
                "clients": self._clients(),
                "client_connections": self._client_connections(status),
                "campaigns": self.campaigns(),
                **self.network(),
            }

    def checkpoint(self) -> None:
        """Flush committed WAL data before a Home Assistant backup."""
        with self.lock:
            result = self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result[0]:
                raise sqlite3.OperationalError("Aruba Monitor database checkpoint is busy")

    def close(self):
        with self.lock:
            self.db.close()
