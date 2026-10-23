import pytest
from conftest import FIXTURES, ap, client

from custom_components.aruba_monitor import parser
from custom_components.aruba_monitor.bands import model_band, primary_channel, resolve_bands
from custom_components.aruba_monitor.entity import build_metrics


@pytest.mark.parametrize("model", ["325(indoor)", "335(indoor)", "535(indoor)", "AP-534", "IAP-324"])
@pytest.mark.parametrize(
    "channel,expected", [("1", "2.4 GHz"), ("14", "2.4 GHz"), ("36+", "5 GHz"), ("149E", "5 GHz")]
)
def test_documented_dual_band_hardware(model, channel, expected):
    assert model_band(model, channel) == expected


@pytest.mark.parametrize("model", ["635(indoor)", "735(indoor)", "555(indoor)", "unknown", None])
def test_no_hardware_range_guessing(model):
    assert model_band(model, "5") is None
    assert model_band(model, "149E") is None


@pytest.mark.parametrize("channel", [None, "", "0", "-1", "999", "36garbage", "5180", "disabled"])
def test_invalid_or_unrecognized_channels(channel):
    assert model_band("535(indoor)", channel) is None


def test_known_ap_and_bonded_client_channel():
    clients, aps = [client(channel="36")], [ap()]
    resolve_bands(clients, aps)
    assert aps[0]["radios"][0]["band"] == "5 GHz"
    assert aps[0]["radios"][0]["band_source"] == "ap_model"
    assert clients[0]["band"] == "5 GHz"
    assert clients[0]["band_source"] == "ap_radio"


def test_client_can_be_resolved_during_channel_change():
    clients, aps = [client(channel="11")], [ap()]
    resolve_bands(clients, aps)
    assert clients[0]["band"] == "2.4 GHz"
    assert clients[0]["band_source"] == "ap_model"


def test_reported_client_and_radio_bands_take_precedence():
    clients = [client(channel="5", band="6 GHz")]
    aps = [ap(radios=[{"index": 0, "channel": "5", "band": "6 GHz"}])]
    resolve_bands(clients, aps)
    assert clients[0]["band"] == "6 GHz"
    assert clients[0]["band_source"] == "reported"
    assert aps[0]["radios"][0]["band"] == "6 GHz"
    assert aps[0]["radios"][0]["band_source"] == "reported"


def test_explicit_radio_band_can_resolve_new_hardware():
    clients = [client(channel="5")]
    aps = [ap(model="635(indoor)", radios=[{"index": 2, "channel": "5", "band": "6 GHz"}])]
    resolve_bands(clients, aps)
    assert clients[0]["band"] == "6 GHz"


@pytest.mark.parametrize("bands", [("2.4 GHz", "6 GHz"), ("2.4 GHz", None)])
def test_overlapping_radio_channels_do_not_guess(bands):
    clients = [client(channel="5")]
    aps = [
        ap(
            model="635(indoor)",
            radios=[{"index": i, "channel": "5", "band": band} for i, band in enumerate(bands)],
        )
    ]
    resolve_bands(clients, aps)
    assert clients[0]["band"] is None


def test_missing_or_duplicate_ap_names_do_not_guess():
    clients = [client(channel="1")]
    resolve_bands(clients, [])
    assert clients[0]["band"] is None
    resolve_bands(clients, [ap(), ap(serial="OTHER", id="OTHER")])
    assert clients[0]["band"] is None


def test_real_capture_resolves_every_client_and_radio(store):
    capture = FIXTURES / "instant_8_10_0_16"
    clients = parser.clients((capture / "clients.txt").read_text())
    aps = parser.aps((capture / "aps.txt").read_text())
    ssids = parser.networks((capture / "networks.txt").read_text())
    resolve_bands(clients, aps)
    assert all(c["band"] in {"2.4 GHz", "5 GHz"} for c in clients)
    assert all(r["band"] in {"2.4 GHz", "5 GHz"} for a in aps for r in a["radios"])
    assert {c["band"] for c in clients} == {"2.4 GHz", "5 GHz"}
    store.ingest(clients, aps, ssids)
    assert all("unknown" not in ssid["by_band"] for ssid in store.network()["ssids"])
    assert sum(sum(ssid["by_band"].values()) for ssid in store.network()["ssids"]) == len(clients)
    metrics = build_metrics(store.snapshot(), "home")
    band_configs = [metric for key, metric in metrics.items() if "_radio_" in key and key.endswith("_band")]
    assert len(band_configs) == sum(len(a["radios"]) for a in aps)
    assert all(metric.value == 0 for key, metric in metrics.items() if "_band_unknown_" in key)
    assert sum(
        metric.value for key, metric in metrics.items() if "_band_2_4_" in key or "_band_5_" in key
    ) == len(clients)


def test_primary_channel_preserves_6ghz_channel_number_without_classifying_it():
    assert primary_channel("5") == 5
    assert primary_channel("149E") == 149
