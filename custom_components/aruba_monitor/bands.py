"""Resolve bands using reported data or documented AP capabilities, never channel alone."""

from __future__ import annotations

import re

# Verified dual-band families, including both antenna variants. Do not extend
# this to a numeric model range: e.g. 6xx/7xx hardware can use overlapping 6 GHz channels.
# https://arubanetworking.hpe.com/techdocs/hardware/aps/ap320/ig/AP-320_Install_Guide_EN.pdf
# https://support.hpe.com/hpesc/public/docDisplay?docId=a00046812en_us
# https://arubanetworking.hpe.com/techdocs/hardware/aps/ap530/ig/AP-530_Install_Guide_EN.pdf
DUAL_BAND_MODELS = frozenset({"324", "325", "334", "335", "534", "535"})


def primary_channel(value: str | None) -> int | None:
    match = re.fullmatch(r"(\d{1,3})(?:[+\-ES])?", value or "", re.IGNORECASE)
    return int(match[1]) if match else None


def model_band(model: str | None, channel: str | None) -> str | None:
    match = re.fullmatch(r"(?:(?:I?AP)-)?(\d{3})(?:\([^)]*\))?", model or "", re.IGNORECASE)
    if not match or match[1] not in DUAL_BAND_MODELS:
        return None
    channel_number = primary_channel(channel)
    if channel_number is not None:
        if 1 <= channel_number <= 14:
            return "2.4 GHz"
        if 32 <= channel_number <= 177:
            return "5 GHz"
    return None


def resolve_bands(clients: list[dict], aps: list[dict]) -> None:
    """Enrich one validated snapshot in place; reported fields always take precedence."""
    by_name = {}
    for ap in aps:
        by_name.setdefault(ap["name"], []).append(ap)
        for radio in ap["radios"]:
            if radio.get("band"):
                radio["band_source"] = "reported"
            else:
                radio["band"] = model_band(ap.get("model"), radio.get("channel"))
                radio["band_source"] = "ap_model" if radio["band"] else None
    for client in clients:
        if client.get("band"):
            client["band_source"] = "reported"
            continue
        client["band_source"] = None
        matches = by_name.get(client["ap"], [])
        if len(matches) != 1:
            continue
        ap = matches[0]
        channel = primary_channel(client.get("channel"))
        if channel is None:
            continue
        candidates = [radio for radio in ap["radios"] if primary_channel(radio.get("channel")) == channel]
        bands = {radio.get("band") for radio in candidates}
        if len(bands) == 1 and None not in bands:
            client["band"] = bands.pop()
            client["band_source"] = "ap_radio"
        elif not candidates:
            # A channel change/roam between the two show commands can miss the
            # radio snapshot. Hardware with no 6 GHz capability still disambiguates it.
            client["band"] = model_band(ap.get("model"), client.get("channel"))
            client["band_source"] = "ap_model" if client["band"] else None
