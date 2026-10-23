from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.aruba_monitor.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def store():
    instance = Store(":memory:")
    yield instance
    instance.close()


@pytest.fixture
def time_at():
    base = datetime.now(UTC) - timedelta(seconds=20)
    return lambda seconds: (base + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def client(mac="aa:bb:cc:00:00:01", ssid="Old Wi-Fi", **extra):
    return {
        "mac": mac,
        "name": "Kitchen speaker",
        "ip": "192.0.2.24",
        "ssid": ssid,
        "ap": "Office",
        "band": None,
        "signal_db": 32,
        "speed_mbps": 120,
        **extra,
    }


def ap(**extra):
    return {
        "id": "SERIAL01",
        "serial": "SERIAL01",
        "name": "Office",
        "ip": "192.0.2.10",
        "clients": 1,
        "model": "535(indoor)",
        "is_vc": True,
        "radios": [
            {
                "index": 0,
                "channel": "36+",
                "band": None,
                "power_dbm": 18,
                "utilization_pct": 4,
                "noise_floor_dbm": -94,
            }
        ],
        **extra,
    }


def inventory():
    return [{"ssid": "Old Wi-Fi"}, {"ssid": "New Wi-Fi"}, {"ssid": "Empty guest"}]
