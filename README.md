# Aruba Monitor for Home Assistant

Monitor Aruba Wi-Fi access points, SSIDs and clients in Home Assistant, with native presence trackers and an admin panel for tracking SSID migrations. Collection is local and read-only.

## Prerequisites

- **Home Assistant 2026.10.0 or newer**.
- One **Aruba Instant AOS-8 virtual controller**. Instant **8.10.0.16** is smoke-tested. Aruba Central, Instant On, AOS-10 and Mobility Controllers are outside this implementation.
- Home Assistant can reach the controller's HTTPS port (default **4343**), with credentials allowed to run `show version`, `show clients`, `show aps` and `show network`.

Enable the REST API from the Instant AP CLI in configuration mode, then save and apply:

```text
(Instant AP)(config)# allow-rest-api
(Instant AP)(config)# end
(Instant AP)# commit apply
```

## Install

**HACS:** Open **Custom repositories**, add `https://github.com/s-bed/ha-aruba-monitor` as an **Integration**, download **Aruba Monitor**, and restart Home Assistant. This is a custom repository, not part of the default HACS catalogue.

**Manual:** Copy `custom_components/aruba_monitor` into `<HA config>/custom_components/aruba_monitor` and restart Home Assistant. You can also extract `aruba_monitor.zip` from the [latest release](https://github.com/s-bed/ha-aruba-monitor/releases/latest) into `<HA config>/custom_components`.

## Configure

1. Open **Settings → Devices & services → Add integration → Aruba Monitor**.
2. Enter the controller hostname/IP **without a URL scheme or port**, credentials and a site ID (default `home`). Keep the site ID stable; it determines entity identities.
3. Set the HTTPS port separately. Keep TLS verification enabled; provide a CA file accessible inside Home Assistant if your controller needs one. The Instant AP IP field is optional and otherwise resolved automatically.

Setup checks the connection and required controller tables. No YAML or extra service is needed.

Use **Reconfigure** on the integration entry to change connection settings or credentials. Use **Options** for polling, tracking and cleanup:

- Polling defaults to **30 seconds**, adjustable from **10–300 seconds**.
- New presence trackers are **disabled by default**. Enable chosen trackers from their device's entity list, or turn on **Track new devices** for future discoveries.
- Inactive client cleanup is **on after 30 days**. Enabled trackers, explicit cleanup protection and campaign references protect clients from automatic deletion. SSID cleanup is **off** by default.

## Use the panel

Open **Aruba Monitor** in the Home Assistant sidebar as an administrator:

- **Clients:** Browse current and retained clients, view their history, and link them to Home Assistant devices by MAC address.
- **Network:** See APs, radios, SSIDs and connection counts. Native entities expose these statistics for dashboards and automations.
- **Migrations:** Choose different source and target SSIDs to track observed devices leaving the source network. Any later non-source SSID counts as migrated; going offline does not. Completion is manual, and 100% covers only observed devices. A password change on the same SSID cannot be verified this way.

Polling continues with the panel closed and all entities disabled. History is stored in `<HA config>/aruba_monitor/aruba.sqlite3` and retained when the integration entry is removed.

## More documentation

- [Settings and operations](docs/reference.md): all options, updates from older versions, device linking, cleanup, campaign rules, availability and backups.
- [Development and releases](docs/development.md): testing, stable releases and PR previews.
