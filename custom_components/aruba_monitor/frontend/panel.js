/* Aruba Monitor: locally bundled Home Assistant sidebar panel, no build step. */
const e = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const stamp = value => value ? new Date(value).toLocaleString(undefined, {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"}) : "Never";
const badge = (value, label = value) => `<span class="badge ${e(value)}">${e(label)}</span>`;
const title = client => client.label || client.name || client.macs[0];
const deviceButton = client => `<button class="device-name" data-device="${e(client.id)}">${e(title(client))}</button>`;
const empty = (heading, text) => `<div class="empty"><h3>${e(heading)}</h3><p class="muted">${e(text)}</p></div>`;
const stat = (value, label) => `<div class="stat"><strong>${e(value)}</strong><span>${e(label)}</span></div>`;

class ArubaMonitorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this.data = null;
    this.inFlight = null;
    this.openDevice = null;
    this.deviceRequest = 0;
    this.campaignQueries = new Map();
    this.campaignStates = new Map();
    this.expandedCampaigns = new Set();
    this.viewScroll = new Map();
    this.view = "clients";
    this.pendingRender = false;
    this.mounted = false;
    this.generation = 0;
    this.timer = null;
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="${new URL("./panel.css", import.meta.url).href}">
      <div class="shell">
      <header>
        <div class="brand"><button class="menu-button" id="toggle-menu" aria-label="Toggle Home Assistant sidebar">☰</button><span class="brand-mark" aria-hidden="true">a</span><h1>Aruba monitor</h1><span id="site-name" class="site-name muted"></span></div>
        <nav aria-label="Dashboard views">
          <button data-view="clients" aria-selected="true">Clients <span id="client-count" class="count">0</span></button>
          <button data-view="network" aria-selected="false">Network</button>
          <button data-view="migrations" aria-selected="false">Migrations</button>
        </nav>
      </header>
      <main>
        <div id="notice" class="notice" role="alert" hidden></div>
        <section id="clients" class="view">
          <div class="section-heading"><h2>Known clients</h2><label class="search-label"><span class="sr-only">Search clients</span><input id="client-search" type="search" placeholder="Search name, MAC, SSID or AP"></label></div>
          <div id="clients-table"></div>
        </section>
        <section id="network" class="view" hidden>
          <div id="network-summary" class="stats"></div>
          <div class="section-heading"><h2>Access points</h2></div><div id="ap-list" class="ap-grid"></div>
          <div class="section-heading"><h2>SSIDs</h2></div><div id="ssid-list"></div>
        </section>
        <section id="migrations" class="view" hidden>
          <div class="section-heading"><h2>Migration campaigns</h2><button id="new-campaign">New campaign</button></div>
          <p class="muted">Progress describes observed devices. Devices never seen by this collector cannot be confirmed.</p>
          <div id="campaign-list"></div>
        </section>
      </main>
      <footer>
        <div id="health" class="health" role="status" aria-live="polite">Connecting to the monitor…</div>
        <span class="auto-refresh">Auto refresh · 5s</span>
      </footer>
      <dialog id="campaign-dialog">
        <form id="campaign-form">
          <div class="section-heading"><h2>New migration campaign</h2><button type="button" data-close="campaign-dialog" class="icon-button" aria-label="Close">×</button></div>
          <label>Campaign name<input name="name" maxlength="100" placeholder="October password rotation" required></label>
          <label>Source SSID<input name="source" list="ssid-options" maxlength="32" required placeholder="Current network"></label>
          <label>Target SSID<input name="target" list="ssid-options" maxlength="32" required placeholder="New network"></label>
          <p class="muted">Source clients join automatically. A sighting on any other SSID after this campaign starts confirms migration.</p>
          <p id="campaign-error" class="error" role="alert"></p>
          <button type="submit">Create campaign</button>
        </form>
      </dialog>
      <dialog id="device-dialog"><div id="device-content"></div></dialog>
      <datalist id="ssid-options"></datalist>
      </div>`;
    this.shadowRoot.addEventListener("click", event => this.onClick(event));
    this.shadowRoot.addEventListener("input", event => this.onInput(event));
    this.shadowRoot.addEventListener("change", event => this.onInput(event));
    this.shadowRoot.addEventListener("submit", event => this.onSubmit(event));
    this.shadowRoot.addEventListener("toggle", event => this.onToggle(event), true);
    this.shadowRoot.addEventListener("focusout", () => queueMicrotask(() => this.renderPending()));
    this.shadowRoot.addEventListener("close", () => this.renderPending(), true);
  }

  set hass(value) {
    this._hass = value;
    this.start();
  }

  get hass() { return this._hass; }

  connectedCallback() {
    this.mounted = true;
    this.start();
  }

  disconnectedCallback() {
    this.mounted = false;
    this.generation += 1;
    this.deviceRequest += 1;
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
  }

  start() {
    if (!this.mounted || !this._hass) return;
    if (this._hass.user?.is_admin === false) {
      if (this.timer !== null) clearInterval(this.timer);
      this.timer = null;
      this.notice("Aruba Monitor requires a Home Assistant administrator account.");
      return;
    }
    if (this.timer !== null) return;
    this.timer = setInterval(() => this.refresh(), 5000);
    this.refresh(true);
  }

  $(selector) { return this.shadowRoot.querySelector(selector); }

  command(name, fields = {}) {
    return this._hass.callWS({type: `aruba_monitor/${name}`, ...fields});
  }

  notice(message) {
    this.$("#notice").textContent = message;
    this.$("#notice").hidden = !message;
  }

  renderHealth() {
    const s = this.data.status;
    this.$("#site-name").textContent = s.site_id ? `/ ${s.site_id}` : "";
    this.$("#health").classList.toggle("warning", !s.fresh);
    this.$("#health").innerHTML = `<strong>${s.fresh ? "● Collection healthy" : "● Collection needs attention"}</strong>
      <span>Last success: ${e(stamp(s.last_success))}</span>
      ${s.firmware ? `<span>Firmware ${e(s.firmware)}</span>` : ""}
      ${s.last_error ? `<span>${e(s.last_error)}</span>` : s.stale ? "<span>Data is stale; current connectivity is unknown.</span>" : ""}`;
  }
  matches(client, query) {
    return [title(client), client.name, client.ip, client.ssid, client.ap, ...client.macs].join(" ").toLowerCase().includes(query.toLowerCase());
  }

  clientRows(clients, campaign = null) {
    return clients.map(client => `<tr>
      <td>${deviceButton(client)}<small class="mono">${client.macs.map(e).join("<br>")}</small></td>
      <td>${badge(client.connection)}<small title="${e(client.last_seen)}">Seen ${e(stamp(client.last_seen))}</small></td>
      <td>${e(client.ssid)}<small>${e(client.ap)} · ${e(client.ip || "No IP")}</small></td>
      ${campaign ? `<td>${badge(client.migration_state)}<small>${client.first_away_seen ? `Away from source ${e(stamp(client.first_away_seen))}` : "Not seen away from source"}${client.exclusion_note ? `<br>${e(client.exclusion_note)}` : ""}</small></td>
      <td><button class="secondary" data-exclude="${e(client.id)}" data-campaign="${e(campaign.id)}" data-excluded="${client.migration_state === "excluded"}" ${campaign.completed_at ? "disabled" : ""}>${client.migration_state === "excluded" ? "Restore" : "Exclude"}</button></td>` :
      `<td>${e(client.band || "Unknown band")}<small>SNR ${e(client.signal_db ?? "—")} dB · ${e(client.speed_mbps ?? "—")} Mbps</small></td>`}
    </tr>`).join("");
  }

  renderClients() {
    const clients = this.data.clients.filter(client => this.matches(client, this.$("#client-search").value));
    this.$("#client-count").textContent = this.data.clients.length;
    this.$("#clients-table").innerHTML = clients.length ? `<div class="card"><div class="table-wrap" data-scroll-key="clients"><table><thead><tr><th>DEVICE / MAC</th><th>CONNECTION</th><th>LAST NETWORK</th><th>LAST RADIO DETAILS</th></tr></thead><tbody>${this.clientRows(clients)}</tbody></table></div></div>` : empty("No matching clients", this.data.clients.length ? "Try another name, SSID, AP or MAC address." : "Clients appear after the first successful collection and stay in this inventory when offline.");
  }

  renderCampaignRows(campaign) {
    const query = this.campaignQueries.get(campaign.id) || "";
    const state = this.campaignStates.get(campaign.id) || "pending-returned";
    const clients = campaign.clients.filter(client => this.matches(client, query) && (
      state === "all" || state === client.migration_state ||
      (state === "pending-returned" && ["pending", "returned"].includes(client.migration_state))
    ));
    return clients.length ? this.clientRows(clients, campaign) : `<tr><td colspan="5" class="muted">${campaign.total ? "No clients match this filter." : "No source clients have been observed yet. Keep collection running to build the list."}</td></tr>`;
  }

  renderCampaigns() {
    this.$("#campaign-list").innerHTML = this.data.campaigns.length ? this.data.campaigns.map(campaign => `<article class="card">
      <div class="card-head"><div><h3>${e(campaign.name)} ${campaign.completed_at ? badge("confirmed", "Completed") : ""}</h3><p class="muted">${e(campaign.source)} → ${e(campaign.target)} · Started ${e(stamp(campaign.created_at))}</p></div>
        <div class="campaign-actions">${campaign.completed_at ? `<span class="caption">Completed ${e(stamp(campaign.completed_at))}</span>` : `<button class="secondary" data-complete="${e(campaign.id)}" ${campaign.ready ? "" : "disabled"}>${campaign.ready ? "Mark complete" : "In progress"}</button>`}<button class="secondary danger" data-delete-campaign="${e(campaign.id)}" aria-label="Delete campaign ${e(campaign.name)}">Delete</button></div></div>
      <div class="stats campaign-stats">${stat(campaign.progress_pct + "%", "Observed progress")}${stat(campaign.counts.pending, "Pending")}${stat(campaign.counts.confirmed, "Confirmed")}${stat(campaign.counts.returned, "Returned to source")}${stat(campaign.counts.excluded, "Excluded")}</div>
      <details class="campaign-clients" data-campaign-details="${e(campaign.id)}" ${this.expandedCampaigns.has(campaign.id) ? "open" : ""}>
      <summary><span class="clients-show">Show clients</span><span class="clients-hide">Hide clients</span> <span class="count">${campaign.total}</span></summary>
      <div class="campaign-search"><input type="search" placeholder="Search campaign clients" aria-label="Search ${e(campaign.name)}" data-campaign-search="${e(campaign.id)}" value="${e(this.campaignQueries.get(campaign.id) || "")}">
      <select data-campaign-state="${e(campaign.id)}" aria-label="Filter migration state">${["pending-returned", "all", "pending", "confirmed", "returned", "excluded"].map(state => `<option value="${state}" ${(this.campaignStates.get(campaign.id) || "pending-returned") === state ? "selected" : ""}>${state === "pending-returned" ? "Pending & returned" : state === "all" ? "All migration states" : state[0].toUpperCase() + state.slice(1)}</option>`).join("")}</select>
      <span class="caption">${campaign.total} known device${campaign.total === 1 ? "" : "s"}</span></div>
      <div class="table-wrap" data-scroll-key="campaign-${e(campaign.id)}"><table><thead><tr><th>DEVICE / MAC</th><th>CONNECTION</th><th>LAST NETWORK</th><th>MIGRATION</th><th>ACTION</th></tr></thead><tbody id="campaign-${e(campaign.id)}">${this.renderCampaignRows(campaign)}</tbody></table></div>
      </details>
      <progress class="campaign-progress" max="100" value="${e(campaign.progress_pct)}" aria-label="${e(campaign.name)}: observed migration progress">${e(campaign.progress_pct)}%</progress>
    </article>`).join("") : empty("No campaigns", "Create a campaign to track observed devices between SSIDs.");
  }

  renderNetwork() {
    this.$("#network-summary").innerHTML = stat(this.data.total_clients, "Clients in latest snapshot") + stat(this.data.aps.filter(ap => ap.online).length, "APs in latest snapshot") + stat(this.data.ssids.length, "Known SSIDs");
    this.$("#ap-list").innerHTML = this.data.aps.length ? this.data.aps.map(ap => `<article class="card"><div class="card-head"><h3>${e(ap.name)} ${ap.is_vc ? '<span class="caption">VC</span>' : ""}</h3>${badge(this.data.status.fresh ? ap.online ? "connected" : "offline" : "unknown", this.data.status.fresh ? ap.online ? "Online" : "Offline" : "Unknown")}</div>
      <div class="ap-meta">${e(ap.model || "Unknown model")} · ${e(ap.ip)}<br>Serial ${e(ap.serial)} · ${e(ap.clients ?? "—")} clients<br>Uplink ${e(ap.uplink || "Unknown")} · Mesh ${e(ap.mesh_role || "Unknown")}</div>
      <div class="table-wrap" data-scroll-key="ap-${e(ap.serial)}"><table><thead><tr><th>RADIO</th><th>CHANNEL</th><th>POWER</th><th>UTIL.</th><th>NOISE</th></tr></thead><tbody>${ap.radios.map(radio => `<tr><td>${e(radio.index)}<small>${e(radio.band || "Unknown band")}</small></td><td>${e(radio.channel)}</td><td>${e(radio.power_dbm ?? "—")} dBm</td><td>${e(radio.utilization_pct ?? "—")}%</td><td>${e(radio.noise_floor_dbm ?? "—")} dBm</td></tr>`).join("") || '<tr><td colspan="5">No reported radio data</td></tr>'}</tbody></table></div></article>`).join("") : empty("No AP observations yet", "Check collection status in the footer.");
    const splits = values => Object.entries(values).map(([name, count]) => `<span>${e(name)}: ${e(count)}</span>`).join("") || '<span>0 clients</span>';
    this.$("#ssid-list").innerHTML = this.data.ssids.length ? `<div class="card table-wrap" data-scroll-key="ssids"><table><thead><tr><th>SSID</th><th>CLIENTS</th><th>BY AP</th><th>BY BAND</th></tr></thead><tbody>${this.data.ssids.map(ssid => `<tr><td>${e(ssid.ssid)}<small>${ssid.in_inventory ? "In latest inventory" : "Historical / configured for a campaign"}</small></td><td>${e(ssid.clients)}</td><td><div class="split">${splits(ssid.by_ap)}</div></td><td><div class="split">${splits(ssid.by_band)}</div></td></tr>`).join("")}</tbody></table></div>` : empty("No SSIDs observed yet", "Configured SSIDs appear even when they have no clients.");
  }

  renderDevice(id, preserveDraft = false) {
    const selectedMac = preserveDraft ? this.$("#alias-mac")?.value : undefined;
    const selectedDevice = preserveDraft ? this.$("#ha-device-target")?.value : undefined;
    const client = this.data.clients.find(item => item.id === id);
    if (!client) { this.$("#device-dialog").close(); this.openDevice = null; return; }
    this.openDevice = id;
    const otherMacs = this.data.clients.filter(item => item.id !== id).flatMap(item => item.macs.map(mac => ({mac, name: title(item)})));
    this.$("#device-content").innerHTML = `<div class="section-heading"><div><h2>${e(title(client))}</h2><p class="muted">First seen ${e(stamp(client.first_seen))}</p></div><button class="icon-button" data-close="device-dialog" aria-label="Close">×</button></div>
      <div class="device-detail-grid">
      <section class="device-column"><h3>Home Assistant devices</h3><div id="device-ha-links" aria-live="polite"><p class="muted">Loading devices…</p></div></section>
      <section class="device-column device-identities">
      <h3>MAC identities</h3><p class="muted">Link only MACs you have verified belong to this device. Migration evidence is recalculated; observations are preserved.</p>
      ${client.macs.map(mac => `<div class="alias-row"><span class="mono">${e(mac)}</span>${client.macs.length > 1 ? `<button class="secondary" data-unlink="${e(mac)}">Unlink</button>` : ""}</div>`).join("")}
      ${otherMacs.length ? `<form id="alias-form"><label for="alias-mac">Link an observed MAC from another device</label><div class="form-row"><select id="alias-mac" name="mac">${otherMacs.map(item => `<option value="${e(item.mac)}">${e(item.name)} · ${e(item.mac)}</option>`).join("")}</select><button>Link MAC</button></div></form>` : '<p class="muted">Other observed MAC addresses will be available to link here.</p>'}
      </section><section class="device-history"><h3>SSID observation history</h3><div class="table-wrap"><table><thead><tr><th>SSID / MAC</th><th>FIRST SEEN</th><th>LAST SEEN</th></tr></thead><tbody>${client.sightings.map(sighting => `<tr><td>${e(sighting.ssid)}<small class="mono">${e(sighting.mac)}</small></td><td>${e(stamp(sighting.first_seen))}</td><td>${e(stamp(sighting.last_seen))}</td></tr>`).join("")}</tbody></table></div></section></div><p id="device-error" class="error" role="alert"></p>`;
    const select = this.$("#alias-mac");
    if (select && Array.from(select.options).some(option => option.value === selectedMac)) select.value = selectedMac;
    return this.loadDeviceLinks(id, selectedDevice);
  }

  renderDeviceLinks(result, selectedDevice) {
    const dialog = this.$("#device-dialog");
    const scroll = dialog.scrollTop;
    const rows = devices => devices.map(device => `<a class="ha-device-card" href="/config/devices/device/${e(encodeURIComponent(device.id))}"><svg class="device-symbol" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/></svg><span class="ha-device-details"><strong>${e(device.name)}</strong><small>${e(device.integration === "aruba_monitor" ? "Aruba Monitor" : device.integration.replaceAll("_", " "))}</small><small class="mono">${device.macs.map(e).join(", ")}</small></span><span class="device-chevron" aria-hidden="true">›</span></a>`).join("");
    const options = result.device_options || [];
    this.$("#device-ha-links").innerHTML =
      (result.devices.length ? rows(result.devices) : '<p class="muted">This client’s Home Assistant device is not registered yet.</p>') +
      '<h3>Linked devices</h3>' +
      (result.linked_devices.length ? rows(result.linked_devices) : '<p class="muted">No linked Home Assistant devices found.</p>') +
      (options.length ? `<form id="ha-device-link-form"><label for="ha-device-target">Link a Home Assistant device</label><div class="form-row"><select id="ha-device-target" name="target_device_id" required><option value="" disabled selected>Choose a device…</option>${options.map(device => `<option value="${e(device.id)}">${e(device.name)} · ${e(device.integration.replaceAll("_", " "))}</option>`).join("")}</select><button>Link device</button></div><p class="caption">Adds this client’s MAC addresses to the selected device in Home Assistant. Choose only a device you recognize as the same hardware.</p></form>` : '<p class="caption">Other Home Assistant devices will appear here when available to link.</p>');
    const select = this.$("#ha-device-target");
    if (select && Array.from(select.options).some(option => option.value === selectedDevice)) select.value = selectedDevice;
    dialog.scrollTop = scroll;
  }

  async loadDeviceLinks(id, selectedDevice) {
    const request = ++this.deviceRequest;
    const generation = this.generation;
    const current = () => this.mounted && this.generation === generation && this.deviceRequest === request && this.openDevice === id && this.$("#device-dialog").open;
    try {
      const result = await Promise.resolve().then(() => this.command("client_devices", {device_id: id}));
      if (!current()) return;
      this.renderDeviceLinks(result, selectedDevice);
    } catch (_error) {
      if (current()) this.$("#device-ha-links").innerHTML = '<p class="error">Could not load Home Assistant devices.</p><button class="secondary" data-retry-device-links="true">Retry</button>';
    }
  }

  editing() {
    return this.shadowRoot.activeElement?.matches("input, select, textarea") ||
      this.$("#campaign-dialog").open || this.$("#device-dialog").open;
  }

  renderPending() {
    if (this.mounted && this.pendingRender && !this.editing()) this.renderSnapshot();
  }

  renderSnapshot() {
    const main = this.$("main");
    const top = main.scrollTop;
    const scroll = new Map(Array.from(this.shadowRoot.querySelectorAll("[data-scroll-key]")).map(element => [element.dataset.scrollKey, element.scrollLeft]));
    this.renderClients();
    this.renderCampaigns();
    this.renderNetwork();
    this.$("#ssid-options").innerHTML = this.data.ssids.map(ssid => `<option value="${e(ssid.ssid)}"></option>`).join("");
    main.scrollTop = top;
    for (const element of this.shadowRoot.querySelectorAll("[data-scroll-key]")) {
      if (scroll.has(element.dataset.scrollKey)) element.scrollLeft = scroll.get(element.dataset.scrollKey);
    }
    this.pendingRender = false;
  }

  refresh(force = false) {
    if (!this.mounted || !this._hass || this._hass.user?.is_admin === false) return Promise.resolve();
    if (this.inFlight) return force ? this.inFlight.then(() => this.refresh(true)) : this.inFlight;
    const generation = this.generation;
    this.inFlight = (async () => {
      try {
        const data = await Promise.resolve().then(() => this.command("snapshot"));
        if (!this.mounted || generation !== this.generation) return;
        this.data = data;
        this.renderHealth();
        if (this.editing()) this.pendingRender = true;
        else this.renderSnapshot();
        this.notice("");
      } catch (error) {
        if (!this.mounted || generation !== this.generation) return;
        this.notice(`Dashboard could not refresh: ${error.message || "Connection failed"}. Displayed observations may be out of date.`);
        this.$("#health").classList.add("warning");
        this.$("#health").textContent = "● Dashboard disconnected — displayed data may be stale.";
      } finally { this.inFlight = null; }
    })();
    return this.inFlight;
  }

  async onClick(event) {
    const button = event.target.closest("button");
    if (!button) return;
    try {
      if (button.id === "toggle-menu") {
        this.dispatchEvent(new CustomEvent("hass-toggle-menu", {bubbles: true, composed: true}));
      } else if (button.dataset.view) {
        const main = this.$("main");
        this.viewScroll.set(this.view, main.scrollTop);
        this.view = button.dataset.view;
        this.shadowRoot.querySelectorAll("nav button").forEach(item => item.setAttribute("aria-selected", item === button));
        this.shadowRoot.querySelectorAll(".view").forEach(view => { view.hidden = view.id !== this.view; });
        main.scrollTop = this.viewScroll.get(this.view) || 0;
      } else if (button.dataset.close) {
        this.$(`#${button.dataset.close}`).close();
        this.renderPending();
      } else if (button.id === "new-campaign") {
        this.$("#campaign-error").textContent = "";
        this.$("#campaign-dialog").showModal();
      } else if (button.dataset.device) {
        const request = this.renderDevice(button.dataset.device);
        this.$("#device-dialog").showModal();
        await request;
      } else if (button.dataset.retryDeviceLinks) {
        button.disabled = true;
        try { await this.loadDeviceLinks(this.openDevice); }
        finally { button.disabled = false; }
      } else if (button.dataset.deleteCampaign) {
        const id = button.dataset.deleteCampaign;
        const campaign = this.data.campaigns.find(item => item.id === id);
        if (!campaign || !window.confirm(`Delete campaign “${campaign.name}”?\n\nThis permanently removes its migration progress and exclusions. Client identities and SSID observation history will be kept.`)) return;
        button.disabled = true;
        try {
          await this.command("campaign_delete", {campaign_id: id});
          this.campaignQueries.delete(id);
          this.campaignStates.delete(id);
          this.expandedCampaigns.delete(id);
          await this.refresh(true);
        } finally { button.disabled = false; }
      } else if (button.dataset.complete) {
        button.disabled = true;
        try {
          await this.command("campaign_complete", {campaign_id: button.dataset.complete});
          await this.refresh(true);
        } finally { button.disabled = false; }
      } else if (button.dataset.exclude) {
        const excluded = button.dataset.excluded !== "true";
        const note = excluded ? window.prompt("Why is this device excluded from this campaign?") : "";
        if (note === null) return;
        button.disabled = true;
        try {
          await this.command("campaign_exclude", {campaign_id: button.dataset.campaign, device_id: button.dataset.exclude, excluded, note});
          await this.refresh(true);
        } finally { button.disabled = false; }
      } else if (button.dataset.unlink) {
        button.disabled = true;
        try {
          await this.command("client_unlink", {device_id: this.openDevice, mac: button.dataset.unlink});
          await this.refresh(true);
          this.renderDevice(this.openDevice, true);
        } finally { button.disabled = false; }
      }
    } catch (error) {
      const deviceError = this.$("#device-error");
      if (this.$("#device-dialog").open && deviceError) deviceError.textContent = error.message || "Request failed";
      else this.notice(error.message || "Request failed");
    }
  }

  onToggle(event) {
    const details = event.target;
    const id = details.dataset?.campaignDetails;
    if (!id || !details.isConnected) return;
    if (details.open) this.expandedCampaigns.add(id);
    else this.expandedCampaigns.delete(id);
  }

  onInput(event) {
    if (!this.data) return;
    if (event.target.id === "client-search") this.renderClients();
    const id = event.target.dataset.campaignSearch || event.target.dataset.campaignState;
    if (!id) return;
    (event.target.dataset.campaignSearch ? this.campaignQueries : this.campaignStates).set(id, event.target.value);
    const campaign = this.data.campaigns.find(item => item.id === id);
    if (campaign) this.$(`#campaign-${id}`).innerHTML = this.renderCampaignRows(campaign);
  }

  async onSubmit(event) {
    event.preventDefault();
    const form = event.target;
    const values = Object.fromEntries(new FormData(form));
    const deviceId = this.openDevice;
    const generation = this.generation;
    let deviceRequest;
    const submit = form.querySelector('button[type="submit"], button:not([type])');
    if (submit) submit.disabled = true;
    try {
      if (form.id === "campaign-form") {
        await this.command("campaign_create", values);
        this.$("#campaign-dialog").close();
        form.reset();
        await this.refresh(true);
      } else if (form.id === "ha-device-link-form") {
        this.$("#device-error").textContent = "";
        deviceRequest = ++this.deviceRequest;
        const result = await this.command("client_device_link", {device_id: deviceId, ...values});
        if (this.mounted && generation === this.generation && deviceRequest === this.deviceRequest && this.openDevice === deviceId && this.$("#device-dialog").open) {
          this.renderDeviceLinks(result, values.target_device_id);
        }
      } else if (form.id === "alias-form") {
        await this.command("client_link", {device_id: this.openDevice, ...values});
        await this.refresh(true);
        this.renderDevice(this.openDevice, true);
      }
    } catch (error) {
      if (form.id === "ha-device-link-form" && (!this.mounted || generation !== this.generation || deviceRequest !== this.deviceRequest || this.openDevice !== deviceId || !this.$("#device-dialog").open)) return;
      this.$(form.id === "campaign-form" ? "#campaign-error" : "#device-error").textContent = error.message || "Request failed";
    } finally { if (submit) submit.disabled = false; }
  }
}

if (!customElements.get("aruba-monitor-panel")) customElements.define("aruba-monitor-panel", ArubaMonitorPanel);
