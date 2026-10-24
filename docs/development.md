# Development and releases

[Back to setup and overview](../README.md)

## Local checks

Use Python 3.14.2 or newer in a separate environment. Development dependencies
are in `requirements-test.txt`.

```sh
python3.14 -m venv .venv-ha
.venv-ha/bin/pip install -r requirements-test.txt
.venv-ha/bin/python -m pytest -q
.venv-ha/bin/ruff check custom_components tests scripts
.venv-ha/bin/ruff format --check custom_components tests scripts
.venv-ha/bin/python scripts/validate_package.py
node --check custom_components/aruba_monitor/frontend/panel.js
node --test tests/test_panel.js
git diff --check
```

Run these checks before submitting changes. Test both the minimum supported
Home Assistant release, **2026.10.0**, and the current test dependency release,
**2026.10.2**, in separate environments. CI also runs official hassfest validation
on pushes and pull requests.

All runtime code and assets belong in `custom_components/aruba_monitor/`.
There is no frontend build step. Sanitized controller captures are in
`tests/fixtures/instant_8_10_0_16/`; the other fixtures and browser mock data are
synthetic and do not establish live controller compatibility. Use isolated
databases for tests. Keep secrets, raw captures, live configuration and
SQLite/WAL/SHM files out of Git and packages.

## Panel API

The administrator-only WebSocket commands use the `aruba_monitor/` namespace:

```text
snapshot             client_devices        client_device_link
client_delete        client_protect        ssid_delete
campaign_create      campaign_complete     campaign_delete
campaign_exclude     client_label          client_link
client_unlink
```

These commands have no controller network-configuration side effects.

## Stable releases

Merging a pull request into `main` automatically publishes a release. The
workflow increments the patch version, updates
`custom_components/aruba_monitor/manifest.json` and `pyproject.toml`, and commits
the version change to `main`. For example, `0.2.0` becomes `0.2.1`. Routine
changes do not need a manual version bump.

To publish manually, open **Actions → Release → Run workflow**. Manual runs
always execute and release trusted `main`, even if another branch is selected
in the dialog. Each new manual run increments the patch version. Rerunning the
same workflow run retries its original release without another bump; rerunning
an older release never moves `latest` back from a newer release.

Each release:

- Uses its version tag as its name, such as **v0.2.1**. Versioned tags let HACS
  detect updates and let users select an older version.
- Includes `aruba_monitor.zip`. Extract it into
  `<HA config>/custom_components`, then restart Home Assistant. HACS installs
  directly from the integration directory in the selected version.
- Becomes GitHub's latest release if it is the newest version. The rolling
  `latest` Git tag points to the same commit.

The [latest release page](https://github.com/s-bed/ha-aruba-monitor/releases/latest)
opens the newest published version.

The workflows use GitHub's built-in token. Repository rules must allow the
release workflow to push its version commit to `main`, create version tags and
update `latest`. Stable releases are serialized; the workflow validates the
package and runs hassfest before publishing refs. The separate hassfest workflow
also validates pushes and pull requests.

## PR previews

The **PR preview** workflow publishes a prerelease named **PR #N preview**, with
a rolling `pr-N` tag and an attached `aruba_monitor.zip`, for open pull requests
targeting `main`. Opening, reopening or adding commits creates or refreshes the
preview. The release notes identify the exact source commit, and the packaged
manifest has a distinct development version identifying that build. Stable
releases and `latest` remain unchanged.

To test a preview, download its attached ZIP, extract the `aruba_monitor` folder
into `<HA config>/custom_components`, and restart Home Assistant. Download the
ZIP again after new commits. Use the attached integration ZIP: GitHub's source
archive also includes trusted `main` files outside the integration.

**Actions → PR preview → Run workflow** accepts a PR number:

- An open PR creates or refreshes its preview.
- A closed PR cleans up its preview, including any preview left after an earlier
  interrupted cleanup. Repeating cleanup is safe.

Merging or closing a PR automatically removes its preview release, attached ZIP
and `pr-N` tag. Reopening it creates the preview again.

### Publishing and cleanup safeguards

Only trusted `main` workflow code runs with release permissions. PR integration
files are read as Git blobs and packaged without executing their code. Packages
contain only integration runtime files; links, submodules, secrets and data files
are rejected.

Publication checks the live PR state and head, and only publishes the current
head of an open PR targeting `main`. Older runs cannot replace a newer preview.
Publication and cleanup share a per-PR queue. Cleanup rechecks that the PR is
still closed and removes only that workflow's preview release and tag,
preserving stable releases, unrelated tags and other PR previews.
