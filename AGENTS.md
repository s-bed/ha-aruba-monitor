# Aruba Monitor: Home Assistant integration

This repository contains the native Home Assistant integration in
`custom_components/aruba_monitor/`. All runtime code and assets must remain inside
that directory for manual and HACS installation. Read `README.md` for setup,
persistence, and operation; use the code and tests to establish current behavior.

Target Home Assistant 2026.10+. Controller support remains local Aruba Instant
AOS-8 only; Instant 8.10.0.16 is the smoke-tested firmware. Sanitized captures are
in `tests/fixtures/instant_8_10_0_16/`; other top-level fixtures are synthetic.
Aruba Central, AOS-10, and Mobility Controllers are outside the implementation.
Observe the network without changing passwords, SSIDs, clients, or AP
configuration. Do not expand claimed firmware/model support without official
documentation and meaningful regression fixtures.

## Integration contracts

Support exactly one controller and one config entry. Use native HA entities and
an admin-only panel and WebSocket operations. Keep site/serial/exact-SSID identity
hashes stable; renaming an AP must not create duplicate entities. Create client entities per
observed MAC, with connectivity enabled and all other client sensors disabled by
default. Register network MAC connections for native HA device linking; never
merge other integrations’ records or infer physical identity from hostnames.
Client entity IDs must survive campaign linking/unlinking and name/SSID changes.
Counts describe observed MAC connections independently from identity grouping.

Read firmware with `show version` at startup. Collect `show clients`, `show aps`,
and `show network` through `/rest/show-cmd` using the documented login/session
flow. Distinguish expired authentication from unsupported commands. Parse columns
using the dashed underline positions, preserving blank names and spaced SSIDs.
Validate rows and reported row counts; footer lines are not clients. A malformed
response must never become an empty successful snapshot.

Retry expired authentication once. For malformed tables, reset the session and
retry the entire snapshot once. Commit clients, APs, and networks atomically only
after every table validates. Failed polls preserve inventory and cannot declare
APs offline or clients absent. Missing APs become offline only after a successful
inventory. Optional metrics are unknown rather than fabricated zero; client
signal is SNR in dB, not RSSI in dBm. Sanitize errors, retaining only safe details
such as command name and response length. Never print authenticated URLs,
session IDs, raw controller responses, passwords, resolved settings, secret
files, or databases. Preserve TLS verification defaults and explicit CA options.

Prefer explicitly reported bands. Documented AP-324/325, AP-334/335, and AP-534/535
capabilities permit 2.4/5 GHz resolution from channels. Match clients using AP
identity and primary channel, respecting bonding suffixes. Keep ambiguous
matches and unsupported hardware unknown. Never classify arbitrary hardware or
6 GHz clients using a channel-number threshold or radio index. Preserve
`band_source` provenance; add documentation and regression cases when extending
the model list.

Polling continues without enabled entities or an open panel. Collection failure
immediately makes panel connectivity unknown, makes client connectivity entities
unavailable, and prevents campaign completion.
Native statistics retain values until stale after three polling intervals; AP
metrics additionally require AP availability; client statistics require that MAC
in the last successful collection. Client last-seen timestamps remain available
as history. Enforce stale deadlines even during reauth. After startup, require a new successful poll before reporting live
state or permitting completion. Load retained campaigns despite controller
outages.

All network/SQLite/CA-file work must avoid blocking HA's event loop. Entity
properties read cached values only. Keep one collector, serialized database
operations, WAL, foreign keys, and atomic transactions. Validate schema before
changing existing databases; never reset or silently downgrade data. Schema
changes require an explicit compatibility and upgrade strategy. The database is
`<HA config>/aruba_monitor/aruba.sqlite3`, outside the installed component. Backup
hooks must pause/drain writes and checkpoint WAL, then resume. Unload/reload must
remove timers/listeners and close the controller/database safely.

Retain devices, MAC aliases, and per-MAC/per-SSID first/last sightings. Do not
replace retained history with current connections or add per-poll snapshots
without a concrete need. Store UTC timestamps and display browser-local times.

## Campaign and panel behavior

Keep Clients first and selected by default, Network second, and Migrations last.
Client details must link to native HA devices and list devices sharing exact
MAC connections without controller polling. The explicit administrator device
picker may add retained client MACs to a selected foreign device’s network
connections, preserving all other metadata and ownership. Reject same-entry MAC
collisions before any registry write; never merge records. Keep this write
blocked during backup/unload. Device cards are fully clickable, the desktop popup
uses two columns with a mobile fallback, and no label input is shown.
Preserve HA theme styling, fixed header/footer with interior scrolling, local
assets, and no frontend build pipeline. Campaign client lists start collapsed and expand independently. Keep
campaign actions and counts visible when collapsed. Default to **Pending &
returned**, with per-campaign filters and four-pixel progress bars. Preserve form
entry, filter/search values, expansion, and scroll state on five-second refresh.
Enforce admin access in every backend command and escape all user/controller
strings before inserting HTML.

- Each campaign has one exact source and target SSID; case and spaces matter.
  Seed enrollment from retained source history and enroll newly observed source
  MACs while active. Offline devices never age out.
- An observation on any SSID other than the exact source after campaign creation
  confirms migration, including SSIDs other than the configured target. Going
  offline alone does not confirm migration; existing confirmation is preserved.
  A source observation at or after the latest non-source observation marks the
  device `returned`; a later non-source sighting confirms it again. Treat
  simultaneous source/non-source sightings conservatively. Keep evidence per
  original MAC/SSID and freeze it on completion. Do not backfill older
  non-source/non-target sightings into campaign evidence. Target timestamps
  refer to the exact target; `first_away_seen`/`last_away_seen` describe any
  non-source SSID.
- Link and unlink observed MACs manually. Evidence remains attached to original
  MACs; recalculate campaign results after identity changes. Device labels and
  MAC links are global, and do not change network connection counts.
- Exclusions require a note and apply only to one campaign. An exclusion on any
  enrolled alias excludes the combined identity. Unlinking retains original
  alias exclusions. Restoring a client returns it to progress calculations.
- Completion is manual and requires fresh collection, at least one included
  device, and no pending or returned devices. Completed campaigns stop gathering
  enrollment/evidence and their exclusions are read-only. Identity corrections
  can still recalculate saved results.
- Active and completed campaigns can be deleted. Confirm deletion in the UI,
  naming the campaign and explaining permanence. Delete the campaign, its
  members/exclusions, and its evidence in one transaction. Preserve global
  devices, labels, MAC links, sightings, network inventory, and other campaigns.
  Cancelled or failed deletion must not clear filters or remove the card.
- Readiness describes observed devices only. Never imply that 100% proves that
  devices never seen by the collector have migrated.

## Verification

Use `requirements-test.txt` in a separate HA-compatible environment. Test released
minimum/current HA versions, not moving development APIs. Local checks are:

```sh
python -m pytest -q
ruff check custom_components tests scripts
ruff format --check custom_components tests scripts
python scripts/validate_package.py
node --check custom_components/aruba_monitor/frontend/panel.js
node --test tests/test_panel.js
git diff --check
```

CI keeps official hassfest validation, stable releases, and PR preview releases. Merged pull
requests and new manual workflow runs automatically bump the patch version and
publish releases. Reruns must reuse the original release and preserve newer
`latest` tags. Manual runs always release trusted `main`; see
`README.md` for the release contract.

PR preview publishing must execute only trusted main code, packaging PR files as
Git blobs. Keep preview tags/releases separate from stable versions and `latest`.
Only publish the current head of an open PR targeting main; older runs must not
roll back newer previews. Package only integration runtime files, rejecting links,
submodules, secrets and data files. Preview manifest versions must identify the
source build. Never execute PR code with the publishing token.

Add meaningful regression coverage for changed parsing, band resolution,
campaign evidence, identity operations, deletion, availability, or storage
behavior. Use isolated databases and synthetic/sanitized data. Do not mutate real
campaigns, restart/deploy services, or publish releases merely to verify changes;
authorization must come from the session. Keep secrets, raw captures, live
configuration, SQLite/WAL/SHM files, and HA test configuration out of Git and
package artifacts.
