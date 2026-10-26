"""Strict fixed-width parsers: a parse failure must never look like zero clients."""

from __future__ import annotations

import ipaddress
import re

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", re.I)


class ParseError(ValueError):
    pass


def number(value: str | None) -> int | float | None:
    match = re.match(r"^(-?\d+(?:\.\d+)?)", value or "")
    if not match:
        return None
    result = float(match[1])
    return int(result) if result.is_integer() else result


def optional(value: str | None) -> str | None:
    return None if value in (None, "", "-", "--", "N/A") else value


def band(value: str | None) -> str | None:
    value = (value or "").lower().replace("ghz", "").strip()
    return {
        "2.4": "2.4 GHz",
        "2.4g": "2.4 GHz",
        "5": "5 GHz",
        "5.0": "5 GHz",
        "5g": "5 GHz",
        "6": "6 GHz",
        "6.0": "6 GHz",
        "6g": "6 GHz",
    }.get(value)


def table(text: str, required: set[str], count_pattern: str | None = None) -> list[dict[str, str]]:
    lines = text.replace("\r", "").expandtabs(8).splitlines()
    for index in range(1, len(lines)):
        underline = lines[index]
        if not re.fullmatch(r"\s*-+(?:\s+-+)*\s*", underline):
            continue
        starts = [match.start() for match in re.finditer(r"-+", underline)]
        ends = starts[1:] + [None]
        names = [lines[index - 1][start:end].strip() for start, end in zip(starts, ends)]
        if required <= set(names):
            break
    else:
        # Some firmware emits only an explicit zero count, without a table header.
        content = [line.strip() for line in lines if line.strip()]
        count = re.fullmatch(count_pattern, content[0], re.I) if count_pattern and len(content) == 1 else None
        if count and count.group(1) == "0":
            return []
        raise ParseError("Missing or unsupported table header")
    rows = []
    for line in lines[index + 1 :]:
        if not line.strip():
            continue
        # Match whole metadata lines: a valid name can start with these words.
        if re.fullmatch(
            r"\s*(?:Number of Clients\s*:\s*\d+|Info timestamp\s*:\s*\d+|"
            r"\d+\s+(?:Access Points|Networks))\s*",
            line,
            re.I,
        ):
            continue
        values = [line[start:end].strip() for start, end in zip(starts, ends)]
        rows.append(dict(zip(names, values)))
    if count_pattern:
        count = next(
            (match for line in lines if (match := re.fullmatch(count_pattern, line.strip(), re.I))), None
        )
        if not count:
            raise ParseError("Missing row count; output may be truncated")
        if len(rows) != int(count[1]):
            raise ParseError("Reported row count does not match parsed table")
    return rows


def clients(text: str) -> list[dict]:
    rows = table(text, {"Name", "MAC Address", "ESSID", "Access Point"}, r"Number of Clients\s*:\s*(\d+)")
    result = []
    for row in rows:
        mac = row["MAC Address"].lower()
        if not MAC_RE.fullmatch(mac) or not row["ESSID"] or not row["Access Point"]:
            raise ParseError("Invalid client row")
        result.append(
            {
                "mac": mac,
                "name": optional(row["Name"]),
                "ip": optional(row.get("IP Address")),
                "ssid": row["ESSID"],
                "ap": row["Access Point"],
                "channel": optional(row.get("Channel")),
                "band": band(row.get("Band")),
                "phy": optional(row.get("Type")),
                "signal_db": number(row.get("Signal")),
                "speed_mbps": number(row.get("Speed (mbps)")),
                "os": optional(row.get("OS")),
            }
        )
    if len({c["mac"] for c in result}) != len(result):
        raise ParseError("Duplicate client MAC addresses")
    return result


def aps(text: str) -> list[dict]:
    rows = table(text, {"Name", "IP Address", "Serial #"}, r"(\d+)\s+Access Points\b")
    result = []
    for row in rows:
        try:
            ipaddress.ip_address(row["IP Address"].rstrip("*"))
        except ValueError as exc:
            raise ParseError("Invalid AP address") from exc
        serial = optional(row["Serial #"])
        if not serial or not row["Name"]:
            raise ParseError("AP name and serial number are required")
        radios = []
        indexes = sorted({int(m[1]) for key in row if (m := re.match(r"radio(\d+) Channel$", key))})
        for index in indexes:
            prefix = f"radio{index} "
            channel = optional(row.get(prefix + "Channel"))
            if channel is None:
                continue
            radios.append(
                {
                    "index": index,
                    "channel": channel,
                    "band": band(row.get(prefix + "Band")),
                    "power_dbm": number(row.get(prefix + "Power (dBm)", row.get(prefix + "Power (dB)"))),
                    "utilization_pct": number(row.get(prefix + "Utilization (%)")),
                    "noise_floor_dbm": number(row.get(prefix + "Noise Floor (dBm)")),
                }
            )
        result.append(
            {
                "id": serial,
                "serial": serial,
                "name": row["Name"],
                "ip": row["IP Address"].rstrip("*"),
                "is_vc": row["IP Address"].endswith("*"),
                "clients": number(row.get("Clients")),
                "model": optional(row.get("Type")),
                "mesh_role": optional(row.get("Mesh Role")),
                "uplink": optional(row.get("Uplink Port", row.get("From Port"))),
                "radios": radios,
            }
        )
    if len({ap["id"] for ap in result}) != len(result):
        raise ParseError("Duplicate AP serial numbers")
    return result


def networks(text: str) -> list[dict]:
    rows = table(text, {"Profile Name", "ESSID", "Clients"})
    result = {}
    for row in rows:
        if not row["ESSID"] or number(row["Clients"]) is None:
            raise ParseError("Invalid network row")
        # Profiles can share an ESSID. Migration identity is the exact ESSID, not the profile name.
        result[row["ESSID"]] = {"ssid": row["ESSID"]}
    return list(result.values())
