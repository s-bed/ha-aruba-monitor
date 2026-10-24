# Configuration and operation reference

[Installation and prerequisites](../README.md#prerequisites) · [Development and releases](development.md)

## Connection settings

Configure Aruba Monitor through Home Assistant; no YAML is required. Setup checks
the controller's firmware and validates the client, AP and network inventories
before accepting the connection.

| Setting | Default | Guidance |
| --- | --- | --- |
| Site ID | `home` | 1–64 letters, digits, hyphens or underscores. Keep it stable: it determines entity identities and cannot be changed through reconfiguration. |
| Virtual controller hostname or IP | Required | Hostname or IP only, without a URL scheme, path or port. |
| HTTPS port | `4343` | Enter separately from the controller address. |
| Instant AP IP address | Automatic | Optional explicit IP for show commands. Otherwise an IPv4 address is resolved from the controller hostname. |
| Username and password | Required | Local controller credentials with access to the required show commands. |
| Verify TLS certificate | Enabled | Validates the controller certificate. |
| CA certificate file path | Empty | Optional CA file accessible inside Home Assistant, using the HA host/container path. |

To change the connection, TLS settings or credentials, open **Settings → Devices
& services → Aruba Monitor**, then choose **Reconfigure** from the entry menu.
Leave the password blank to keep the saved password. Rejected credentials during
operation start Home Assistant's reauthentication flow.

## Options

Open **Options** on the integration entry to change these settings.

| Option | Default | Allowed values or behavior |
| --- | --- | --- |
| Track new devices | Off | Enables presence trackers when first registered. Existing entity enable/disable choices are preserved. |
| Polling interval | 30 seconds | 10–300 seconds. |
| HTTP request timeout | 15 seconds | Any positive number of seconds, applied to each request. |
| Automatically delete inactive clients | On | Deletes eligible absent clients after successful collections. See [cleanup](#cleanup-and-deletion). |
| Client inactivity period | 30 days | Positive whole number of days. |
| Automatically delete inactive SSIDs | Off | Deletes eligible SSIDs no longer reported or used by observed clients. |
| SSID inactivity period | 30 days | Positive whole number of days, independent of client retention. |

Polling continues with all entities disabled and the panel closed. Monitoring
pauses while Home Assistant is stopped. Use one collector per controller.

### Updating an existing installation

Current configuration entries use version 2 and retain their saved settings on
update. Older entry versions are not converted automatically: remove the entry
from **Settings → Devices & services**, then add it again with the same site ID.
The database and campaign history remain saved when the entry is removed.
Obsolete entities from earlier releases need to be removed manually if desired.

## Devices and entities

Aruba Monitor creates native Home Assistant entities for:

- Collector connectivity, last successful collection and total MAC connections.
- AP connectivity and client counts; reported radio band, channel, power,
  utilization and noise floor.
- SSID client totals and splits by AP and by 2.4/5/6 GHz or unknown band.
- One device tracker per observed client MAC, reporting `home` while connected
  and `not_home` after leaving. Trackers are disabled by default. Enable selected
  trackers from the device's entity list, or turn on **Track new devices** for
  future discoveries.
- Optional client IP address, SSID, access point, band, channel, SNR, link speed
  and last-seen sensors, also disabled by default.

Missing metrics stay unknown. Client signal is signal-to-noise ratio (SNR) in dB.
Bands use controller-reported values first. Missing client bands can be resolved
from the matching AP and primary radio channel, including bonded-channel
suffixes. Channel inference based on hardware is limited to AP-324/325,
AP-334/335 and AP-534/535. Other hardware needs a reported band or an unambiguous
match to a radio with a reported band. Unresolved bands remain unknown.

Client entities keep their IDs when names, SSIDs or saved identity links change,
and remain registered after disconnection. Renaming an AP also preserves its
entities. Network counts represent observed MAC connections; saved identity
groups affect campaign progress without combining those counts or client
entities.

## Sidebar panel and Home Assistant linking

The **Aruba Monitor** sidebar panel and its backend operations are available to
administrators only. It follows the HA theme and refreshes every five seconds
without polling the controller itself.

- **Clients** opens first. Browse retained clients and per-MAC/SSID history;
  click a client for details and links to its Home Assistant devices.
- **Network** shows APs, radios, SSIDs and connection counts. SSIDs with no
  clients are still discovered. Missing SSIDs remain in retained inventory until
  deleted, with current statistics unavailable.
- **Migrations** contains SSID migration campaigns. Client lists start collapsed
  with the **Pending & returned** filter. Searches, filters, form values and
  expanded lists survive automatic refreshes.

Home Assistant automatically links device records that expose the same network
MAC. Aruba Monitor uses exact MAC matches, never hostnames. Randomized MACs and
different interface MACs remain separate unless a saved identity relationship
already exists.

In client details, **Link a Home Assistant device** lets an administrator select
another integration's device and add the client's retained MAC addresses to its
network connections. This preserves that device's metadata and ownership; it
does not move entities or merge registry records. Conflicting MAC ownership
within the selected device's integration is rejected. Existing identity groups,
labels and history remain supported, but the panel has no MAC grouping controls.

## Cleanup and deletion

Automatic cleanup runs only after a successful collection. It deletes absent
clients or SSIDs once their last observation reaches the configured inactivity
period. Startup without a fresh collection and controller outages never trigger
cleanup.

Clients are protected from automatic deletion when they have an enabled presence
tracker, belong to any saved campaign, or have **Protect from automatic cleanup**
enabled in their details. Tracker protection applies to the entire retained
identity. Disabling the tracker removes that exemption.

An administrator can manually delete an inactive client or SSID after a fresh
collection confirms its absence. The panel asks for confirmation. Explicit
deletion is allowed even for clients protected from automatic cleanup.

- Deleting a client removes its retained MAC history, Aruba Monitor entities and
  association, plus its membership, exclusions and evidence from every campaign.
  Campaign progress is recalculated. Other clients and Home Assistant device
  records owned by other integrations remain unchanged, including their network
  connections.
- Deleting an SSID removes its local inventory and Aruba Monitor entities.
  Client sightings, campaign evidence and historical SSID suggestions remain.

Deleted items are recreated as newly observed items if detected again.

## Collection failures and restarts

A failed collection immediately makes panel connectivity unknown and blocks
campaign completion and deletion. Inventory is preserved, so a failed poll
cannot mark APs offline or clients absent.

Trackers keep their last `home`/`not_home` state through two failed complete
collection attempts, after internal retries. The third failure makes them
unavailable until recovery. The outage and recovery are each logged once.
Rejected credentials make trackers unavailable immediately. A client that simply
leaves the network remains available as `not_home`.

Native statistics retain values until stale after three polling intervals,
including during reauthentication. AP statistics also require the AP to be
online; client statistics require that MAC in the last successful collection.
Last-seen timestamps remain available as history.

After a restart, retained tracker states and campaigns load even during an
outage. Restored tracker states are historical until a successful poll. Panel
connectivity, campaign completion and deletion require a new successful
collection after startup; historical last-success timestamps remain visible.

## Campaign rules

Create a named campaign with different source and target SSIDs. Matching is
exact, including case and spaces. A password change on the same SSID cannot be
verified from these observations.

- **Enrollment:** campaigns include retained source history and newly observed
  source MACs while active. Offline members never age out, including when
  automatic cleanup is enabled.
- **Confirmed:** any observation on a non-source SSID after creation confirms
  migration, including a different SSID from the target. Older non-source
  sightings do not establish evidence. Going offline alone neither confirms
  migration nor clears an existing confirmation.
- **Returned:** a source sighting at or after the latest non-source sighting
  marks the device returned, including simultaneous sightings. A later
  non-source observation confirms it again.
- **Exclusions:** require a note and apply only to that campaign. An exclusion on
  any enrolled alias excludes its combined identity. Restoring it returns the
  identity to progress calculations; unlinking preserves exclusions on the
  original MACs.
- **Completion:** manual, requiring fresh collection, at least one included
  device and no pending or returned devices. Completed campaigns stop enrolling
  clients and gathering evidence, and their exclusions become read-only.
  Identity corrections can still recalculate saved results.
- **Deletion:** active and completed campaigns can be deleted after confirmation
  by name. This permanently removes the campaign, membership, exclusions and
  evidence together, preserving global devices, identities, labels, sightings,
  inventory and other campaigns.

Evidence stays attached to the original observed MAC. Target timestamps refer to
the exact target; away timestamps cover any non-source SSID. Timestamps are
stored in UTC and displayed in your browser's local time.

**100% means the observed enrolled devices have migrated. It does not prove that
unseen devices have migrated.**

## Persistent data and backups

The database is `<HA config>/aruba_monitor/aruba.sqlite3`, outside
`custom_components` and unaffected by HACS upgrades. It retains devices, MAC
aliases and per-MAC/per-SSID first and last sightings. Unsupported database
schemas are rejected without resetting data.

Removing the integration entry keeps the database. Delete its directory manually
only if you intend to discard history.

Home Assistant backup hooks pause writes, drain active database work and
checkpoint SQLite WAL before the backup, then resume collection. Ensure the
backup includes the HA configuration directory. For a manual backup, stop Home
Assistant or use SQLite's online backup API; copying only the main database file
while WAL is active is unsafe. Restore with Home Assistant stopped, correct file
ownership, and no leftover WAL/SHM files from another database.
