# Aruba Monitor for Home Assistant

A native, local-polling Home Assistant integration for an **Aruba Instant AOS-8 virtual controller**. Monitor APs, radios and SSIDs, and track devices during an SSID/password migration directly in Home Assistant.

The integration observes the network. It never changes SSIDs, rotates passwords, disconnects clients or configures APs. Instant **8.10.0.16** has been smoke-tested; other firmware is not established as supported by synthetic tests alone. Aruba Central, Instant On, AOS-10 and Mobility Controllers are outside this implementation.

## Install

Requires **Home Assistant 2026.10.0 or newer**. One controller/integration entry is supported.

### HACS custom repository

1. In HACS, open **Custom repositories**, add `https://github.com/s-bed/ha-aruba-monitor`, and select **Integration**.
2. Download **Aruba Monitor** and restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → Aruba Monitor**.

This integration is available as a HACS custom repository; it is not listed in the default HACS catalogue.

### Manual

Copy `custom_components/aruba_monitor` to `<HA config>/custom_components/aruba_monitor`, then restart Home Assistant and add the integration. Everything needed at runtime, including the panel assets, is inside that folder. There is no frontend build step.

## Configure

Enter the controller hostname/IP (without a URL scheme or port), username, password and site ID. Keep the site ID stable because it determines entity identities. The default is `home`, and it cannot be changed through reconfiguration.

Connection settings include port (default `4343`), an optional explicit Instant AP IPv4 address, TLS verification (enabled by default), and an optional CA certificate file accessible inside Home Assistant. Custom CA paths refer to the HA host/container filesystem, not the computer running your browser. The default HTTPS timeout is 15 seconds.

Setup validates the login and all required controller tables before accepting the entry. Use **Reconfigure** for connection/TLS/credential changes and **Options** for the collection interval (default 30 seconds, minimum 5) and timeout. Invalid credentials during operation start Home Assistant's reauthentication flow.

Do not run two collectors against the same controller during normal use. Monitoring pauses when Home Assistant is stopped or restarted.

## Devices, entities and panel

The **Aruba Monitor** sidebar panel is visible to administrators only. Its authenticated backend also restricts all client/campaign reads and writes to administrators.

- **Clients** opens first: retained device inventory, explicitly linked MAC identities and per-MAC/SSID observation history. Click a client for a wider, two-column details popup on desktop, with a stacked layout on smaller screens. Click a device card to open it in Home Assistant. Linked devices appear as cards too. Grouped clients show the devices for each linked MAC. Devices do not age out merely because they are offline.
- **Network:** APs, reported radios, SSID counts and connection splits. The health footer remains visible while the content scrolls.
- **Migrations** is last: create campaigns with exact source/target SSIDs; expand each campaign's clients independently. Lists start collapsed and default to **Pending & returned**. Searches, filters and form entry survive automatic refreshes.

The panel follows the active HA theme and refreshes every five seconds while mounted. These reads do not poll the controller. All frontend assets are local.

The integration provides native entities for:

- Collector connectivity health, last successful collection, and total MAC connections.
- AP connectivity, client count, and each reported radio's band, channel, power, utilization and noise floor.
- SSID client totals and splits by AP and by 2.4/5/6 GHz or unknown band.
- A **Wi-Fi connected** binary sensor for every observed client MAC, enabled by default. Optional IP address, SSID, access point, band, channel, signal-to-noise ratio, link speed and last-seen sensors are disabled by default; enable them from the device’s entity list.

Client devices advertise their network MAC to Home Assistant, which automatically links them to devices from other integrations exposing the same MAC, including devices discovered later. Home Assistant retains each integration’s device record and metadata. In the popup, choose a device under **Link a Home Assistant device** and select **Link device** to associate it manually. This adds the client’s observed MAC addresses to that device’s network connections, making the link visible in HA as well as Aruba Monitor; it does not move entities or merge registry records. Conflicting MAC ownership within the selected device’s integration is rejected. The unused device-label input is removed; existing saved labels remain in history. Automatic matching uses the exact MAC, never a hostname; devices with randomized or different interface MACs require separate identities.

Client entities retain stable IDs when names, SSIDs or campaign identity links change. They remain registered when clients disconnect. Linking identities in the panel changes campaign grouping, not individual MAC entities or network connection counts. AP IDs use serial hashes; SSID IDs hash the exact UTF-8 SSID. AP renames preserve entity identities. Missing metrics remain unknown. Client signal is SNR in dB.

A failed poll immediately marks collection unhealthy, makes client connectivity entities unavailable and panel connectivity unknown, and prevents campaign completion. Native statistics retain their last values until collection is stale after three polling intervals; AP statistics additionally require the AP to be online; client statistics require that MAC to be connected in the last successful collection. Client last-seen timestamps remain available as historical values. A separate timer enforces staleness even during reauthentication. After every integration startup, live connectivity and completion remain blocked until that startup's first successful poll. Historical last-success timestamps remain visible.

## Campaign rules

Campaigns enroll retained source history and newly observed source MACs while active. Any observation on a different SSID after creation confirms migration, including a non-target SSID. A source observation at or after the latest non-source sighting marks the device returned. Offline status alone never confirms migration, and offline devices never age out.

Exclusions require a note and apply only to their campaign. Linking/unlinking observed MACs recalculates campaign results without moving evidence away from its original MAC. Completed campaigns freeze enrollment/evidence; their exclusions are read-only, while identity corrections can still recalculate saved results.

Completion is manual and requires fresh collection, at least one included device and no pending/returned devices. Deleting an active or completed campaign removes its membership, exclusions and evidence atomically, preserving global labels, identities, sightings, inventory and other campaigns. The panel confirms deletion by name.

**100% means the observed enrolled devices have migrated. It does not prove that unseen devices have migrated.**

## Persistent data and backups

The database is `<HA config>/aruba_monitor/aruba.sqlite3`, outside `custom_components` and unaffected by HACS upgrades. The integration retains SQLite schema version 1, transactions, foreign keys and WAL. Unsupported schemas are rejected without resetting data. Removing the integration entry retains its database; delete that directory manually only if you intend to discard history.

HA backup hooks pause writes, drain active database work and checkpoint WAL before the backup, then resume collection afterward. Ensure your backup includes the HA configuration directory. For manual backups, stop HA or use SQLite's online backup API; copying only the main SQLite file while WAL is active is unsafe. Restore with HA stopped, including correct filesystem ownership and no leftover WAL/SHM files from another database.

## Development and verification

The integration uses the Python version required by HA 2026.10 (3.14.2+). Development dependencies are in `requirements-test.txt`.

```sh
python3.14 -m venv .venv-ha
.venv-ha/bin/pip install -r requirements-test.txt
.venv-ha/bin/python -m pytest -q
.venv-ha/bin/ruff check custom_components tests scripts
.venv-ha/bin/ruff format --check custom_components tests scripts
.venv-ha/bin/python scripts/validate_package.py
node --check custom_components/aruba_monitor/frontend/panel.js
node --test tests/test_panel.js
```

CI runs official hassfest validation. Run the local checks above before submitting changes, and test against both HA 2026.10.0 and 2026.10.2 using separate environments. Sanitized real controller fixtures are in `tests/fixtures/instant_8_10_0_16`. Synthetic tests and browser mock data do not establish live controller compatibility.

The panel's administrator-only WebSocket commands are namespaced under `aruba_monitor/`: `snapshot`, `client_devices`, `client_device_link`, `campaign_create`, `campaign_complete`, `campaign_delete`, `campaign_exclude`, `client_label`, `client_link`, and `client_unlink`. Panel commands have no network-configuration side effects.

## Releases

Merging a pull request into `main` automatically publishes a release. The workflow increments the patch version, updates `custom_components/aruba_monitor/manifest.json` and `pyproject.toml`, and commits the version change to `main`. For example, `0.2.0` becomes `0.2.1`. No manual version bump is needed for routine changes.

To publish manually, open **Actions → Release → Run workflow**. Manual runs always release `main`, even if another branch is selected in the run dialog. Each new manual run increments the patch version; rerunning the same workflow run retries its original release without another bump.

Each release is named **latest**, has a permanent version tag such as `v0.2.1`, and is marked as GitHub’s latest release. The [latest release page](https://github.com/s-bed/ha-aruba-monitor/releases/latest) always opens the newest published version. A rolling `latest` Git tag points to the same commit. Versioned release tags let HACS detect updates and let you select an older version.

Each release includes `aruba_monitor.zip`. Extract it into `<HA config>/custom_components` to install its `aruba_monitor` folder, then restart Home Assistant. HACS installs directly from the integration directory in the selected version.

The workflows use GitHub's built-in token. Repository rules must allow the release workflow to push its version commit to `main`, create version tags and update `latest`. The separate hassfest workflow validates pushes and pull requests.

### PR test builds

The **PR preview** workflow creates a prerelease named **PR #N preview**, with a rolling `pr-N` tag and `aruba_monitor.zip`, when a pull request targeting `main` is opened or reopened. New commits replace that PR's ZIP with the current integration files. The release notes identify the exact source commit, and the packaged manifest has a distinct development version. Stable releases and `latest` remain unchanged.

Download the ZIP from the PR's prerelease, extract its `aruba_monitor` folder into `<HA config>/custom_components`, and restart Home Assistant. Download the ZIP again after a new commit to test the updated preview. The prerelease's source archive includes trusted main files outside the integration; use the attached integration ZIP for testing.

The workflow must first be merged into `main` before GitHub activates it. After that, **Actions → PR preview → Run workflow**, with an open PR number, can create or refresh a preview for a PR that was already open. Only trusted publishing code runs with release permissions; PR integration files are packaged without executing their code.
