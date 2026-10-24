"""Validate installable contents and metadata without importing Home Assistant."""

from __future__ import annotations

import ast
import json
import re
import struct
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "aruba_monitor"
COMPONENT = ROOT / "custom_components" / DOMAIN
HA_IMPORTS = {"homeassistant", "aiohttp", "probatio"}
REQUIRED_FILES = (
    "__init__.py",
    "manifest.json",
    "config_flow.py",
    "coordinator.py",
    "sensor.py",
    "binary_sensor.py",
    "device_tracker.py",
    "strings.json",
    "translations/en.json",
    "frontend/panel.js",
    "frontend/panel.css",
    "brand/icon.png",
)


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(), object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError(f"{path.relative_to(ROOT)} must contain a JSON object")
    return value


def validate() -> None:
    """Fail when copying only the integration folder would omit runtime code."""
    folders = sorted(
        path.name
        for path in COMPONENT.parent.iterdir()
        if path.is_dir() and not path.name.startswith((".", "__"))
    )
    if folders != [DOMAIN]:
        raise ValueError("Exactly one integration must be under custom_components")
    for filename in REQUIRED_FILES:
        if not (COMPONENT / filename).is_file():
            raise ValueError(f"Missing integration file: {filename}")
    for path in COMPONENT.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Integration contains a symlink: {path.relative_to(COMPONENT)}")
        if path.is_file() and (
            path.name == ".env"
            or path.suffix.lower() in {".db", ".key", ".p12", ".pfx"}
            or ".sqlite" in path.name
        ):
            raise ValueError(f"Unexpected data/secret file: {path.relative_to(COMPONENT)}")

    manifest = read_json(COMPONENT / "manifest.json")
    hacs = read_json(ROOT / "hacs.json")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if manifest.get("domain") != DOMAIN:
        raise ValueError("Manifest domain must match the integration folder")
    for key in ("name", "documentation", "issue_tracker", "codeowners", "version"):
        if not manifest.get(key):
            raise ValueError(f"Missing manifest field: {key}")
    if not re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]):
        raise ValueError("Integration version must have major.minor.patch form")
    if manifest["version"] != project["version"]:
        raise ValueError("Development project and integration versions differ")
    if manifest.get("config_flow") is not True or manifest.get("single_config_entry") is not True:
        raise ValueError("The integration must provide a singleton config flow")
    if manifest.get("iot_class") != "local_polling" or manifest.get("integration_type") != "hub":
        raise ValueError("The integration must describe a locally polled hub")
    if manifest.get("requirements") != []:
        raise ValueError("Review package import allowlist when adding runtime requirements")
    if hacs.get("content_in_root", False):
        raise ValueError("HACS must install from custom_components, not the repository root")
    if hacs.get("name") != manifest["name"]:
        raise ValueError("HACS and manifest names differ")
    if not re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", hacs.get("homeassistant", "")):
        raise ValueError("HACS metadata must declare the minimum Home Assistant release")
    if hacs.get("zip_release", False):
        raise ValueError("This repository installs directly from its integration folder")
    for key in ("documentation", "issue_tracker"):
        if not manifest[key].startswith("https://github.com/s-bed/ha-aruba-monitor"):
            raise ValueError(f"Unexpected repository URL in {key}")
    strings = read_json(COMPONENT / "strings.json")
    for path in COMPONENT.rglob("*.json"):
        read_json(path)
    if read_json(COMPONENT / "translations/en.json") != strings:
        raise ValueError("English translations must match strings.json")

    icon = (COMPONENT / "brand/icon.png").read_bytes()
    if icon[:8] != b"\x89PNG\r\n\x1a\n" or len(icon) < 24:
        raise ValueError("Brand icon must be a PNG image")
    if struct.unpack(">II", icon[16:24]) != (256, 256):
        raise ValueError("Brand icon must be 256 by 256 pixels")

    for path in COMPONENT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        parent_depth = len(path.parent.relative_to(COMPONENT).parts)
        for node in ast.walk(tree):
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > parent_depth + 1:
                        raise ValueError(f"Relative import escapes integration: {path.name}:{node.lineno}")
                    base = path.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    if node.module:
                        target = base.joinpath(*node.module.split("."))
                        if not target.with_suffix(".py").is_file() and not (target / "__init__.py").is_file():
                            raise ValueError(f"Missing bundled module: {path.name}:{node.lineno}")
                    continue
                if node.module:
                    imports = [node.module]
            for module in imports:
                if module.split(".")[0] not in sys.stdlib_module_names | HA_IMPORTS:
                    raise ValueError(f"Unbundled import {module}: {path.name}:{node.lineno}")


if __name__ == "__main__":
    try:
        validate()
    except (OSError, ValueError, SyntaxError, KeyError) as error:
        print(f"Package validation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    print("Integration package and local metadata are valid.")
