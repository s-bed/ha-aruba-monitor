import pytest
from conftest import FIXTURES

from custom_components.aruba_monitor import parser


def text(name):
    return (FIXTURES / name).read_text()


def test_clients_with_spaces_blank_name_and_footer():
    result = parser.clients(text("clients.txt"))
    assert len(result) == 3
    assert result[0]["name"] == "Living room speaker"
    assert result[1]["name"] is None
    assert result[0]["ssid"] == "Old Wi-Fi"
    assert result[0]["signal_db"] == 33
    assert result[0]["speed_mbps"] == 120
    assert result[0]["band"] is None  # Channel alone cannot distinguish 2.4 GHz and 6 GHz.


def test_aps_optional_radios_and_power_header_variants():
    result = parser.aps(text("aps.txt"))
    assert len(result) == 2
    assert result[0]["is_vc"]
    assert result[0]["radios"][0]["power_dbm"] == 18
    assert result[0]["radios"][1]["noise_floor_dbm"] == -96
    assert result[1]["radios"] == []
    assert (
        parser.aps(text("aps.txt").replace("Power (dBm)", "Power (dB) "))[0]["radios"][0]["power_dbm"] == 18
    )


def test_networks_including_empty_and_spaced_ssids():
    result = parser.networks(text("networks.txt"))
    assert result == [{"ssid": "Old Wi-Fi"}, {"ssid": "New Wi-Fi"}, {"ssid": "Empty guest"}]


def test_empty_client_and_ap_tables():
    assert parser.clients(text("clients_empty.txt")) == []
    assert parser.aps("0 Access Points\n") == []


@pytest.mark.parametrize("output", ["", "REST API Service is not enabled", "Unknown table\n----\nabc"])
def test_missing_header_is_never_empty_snapshot(output):
    with pytest.raises(parser.ParseError):
        parser.clients(output)


def test_malformed_row_rejects_entire_table():
    with pytest.raises(parser.ParseError, match="Invalid client"):
        parser.clients(text("clients.txt").replace("aa:bb:cc:00:00:01", "not-a-valid-mac!!!"))


def test_truncated_output_rejected():
    with pytest.raises(parser.ParseError, match="row count"):
        parser.clients(text("clients.txt").replace("Number of Clients :3", "Number of Clients :4"))


def test_duplicate_macs_rejected():
    with pytest.raises(parser.ParseError, match="Duplicate"):
        parser.clients(text("clients.txt").replace("aa:bb:cc:00:00:02", "aa:bb:cc:00:00:01"))


def test_missing_client_footer_is_not_a_valid_empty_poll():
    with pytest.raises(parser.ParseError, match="Missing row count"):
        parser.clients(text("clients_empty.txt").split("Number of Clients")[0])


@pytest.mark.parametrize(
    "value,expected", [("6 GHz", "6 GHz"), ("2.4", "2.4 GHz"), ("all", None), (None, None)]
)
def test_explicit_bands_only(value, expected):
    assert parser.band(value) == expected


def test_sanitized_live_instant_8_10_0_16_capture():
    capture = FIXTURES / "instant_8_10_0_16"
    clients = parser.clients((capture / "clients.txt").read_text())
    aps = parser.aps((capture / "aps.txt").read_text())
    ssids = parser.networks((capture / "networks.txt").read_text())
    assert len(clients) == 50
    assert len(aps) == 4
    assert len(ssids) == 5
    assert {c["ap"] for c in clients} <= {ap["name"] for ap in aps}
    assert {c["ssid"] for c in clients} <= {s["ssid"] for s in ssids}
    assert all(ap["serial"].startswith("LABSER") for ap in aps)
    assert all(ap["radios"] for ap in aps)
    assert all(radio["noise_floor_dbm"] is not None for ap in aps for radio in ap["radios"])
