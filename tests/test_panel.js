// These isolated panel tests use synthetic snapshots and never touch live campaigns.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function setup(confirm = true) {
  const nodes = new Map();
  const calls = [];
  const intervals = new Map();
  let intervalId = 0;
  const root = {
    innerHTML: "", activeElement: null,
    addEventListener() {}, querySelectorAll: () => [],
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, {
        value: "", innerHTML: "", textContent: "", hidden: false, open: false,
        scrollTop: 0, scrollLeft: 0, options: [],
        classList: {add() {}, toggle() {}},
        close() { this.open = false; }, showModal() { this.open = true; },
      });
      return nodes.get(selector);
    },
  };
  const definitions = new Map();
  const context = vm.createContext({
    URL, queueMicrotask, CustomEvent: class {},
    HTMLElement: class { attachShadow() { this.shadowRoot = root; } dispatchEvent() {} },
    customElements: {get: name => definitions.get(name), define: (name, value) => definitions.set(name, value)},
    setInterval: (callback, ms) => { intervals.set(++intervalId, {callback, ms}); return intervalId; },
    clearInterval: id => intervals.delete(id),
    window: {
      confirm: message => { calls.push(["confirm", message]); return confirm; },
      prompt: () => "A verified test exclusion",
    },
    FormData: class { constructor(form) { return new Map(Object.entries(form.fields)); } },
  });
  const source = fs.readFileSync(path.join(__dirname, "../custom_components/aruba_monitor/frontend/panel.js"), "utf8");
  vm.runInContext(source.replaceAll("import.meta.url", '"https://ha.example/aruba_monitor/panel.js"'), context);
  const panel = new (definitions.get("aruba-monitor-panel"))();
  panel._hass = {user: {is_admin: true}, callWS: async message => { calls.push(["ws", JSON.parse(JSON.stringify(message))]); return snapshot(); }};
  panel.mounted = true;
  panel.data = snapshot();
  return {panel, root, nodes, calls, intervals, context};
}

function campaign() {
  return {
    id: "test-campaign", name: "October rotation", source: "Old", target: "New", total: 4,
    progress_pct: 33.3, counts: {pending: 1, returned: 1, confirmed: 1, excluded: 1},
    clients: ["pending", "returned", "confirmed", "excluded"].map((state, i) => ({
      id: `device-${i}`, name: `${state} device`, macs: [`aa:bb:cc:00:00:0${i}`],
      migration_state: state, connection: "offline", sightings: [],
    })),
  };
}
function snapshot() {
  return {status: {fresh: true, site_id: "home"}, campaigns: [campaign()], clients: campaign().clients, aps: [], ssids: [], total_clients: 0};
}
function click(panel, dataset, id = "") {
  const button = {dataset, id, disabled: false};
  return {button, run: () => panel.onClick({target: {closest: () => button}})};
}
function filters(panel) {
  panel.campaignQueries.set("test-campaign", "filter");
  panel.campaignStates.set("test-campaign", "pending");
  panel.expandedCampaigns.add("test-campaign");
}

test("cancelled deletion names the campaign, explains permanence, and preserves filters", async () => {
  const {panel, calls} = setup(false);
  filters(panel);
  const action = click(panel, {deleteCampaign: "test-campaign"});
  await action.run();
  assert.equal(calls.length, 1);
  assert.match(calls[0][1], /October rotation/);
  assert.match(calls[0][1], /permanently/);
  assert.match(calls[0][1], /history will be kept/);
  assert.equal(panel.campaignQueries.get("test-campaign"), "filter");
  assert.equal(panel.expandedCampaigns.has("test-campaign"), true);
  assert.equal(action.button.disabled, false);
});

test("successful deletion uses authenticated HA command then clears only its saved filters", async () => {
  const {panel, calls} = setup();
  filters(panel);
  panel.campaignQueries.set("other", "keep");
  panel.refresh = async force => calls.push(["refresh", force]);
  const action = click(panel, {deleteCampaign: "test-campaign"});
  await action.run();
  assert.deepEqual(calls.slice(1), [["ws", {type: "aruba_monitor/campaign_delete", campaign_id: "test-campaign"}], ["refresh", true]]);
  assert.equal(panel.campaignQueries.has("test-campaign"), false);
  assert.equal(panel.campaignStates.has("test-campaign"), false);
  assert.equal(panel.expandedCampaigns.has("test-campaign"), false);
  assert.equal(panel.campaignQueries.get("other"), "keep");
  assert.equal(action.button.disabled, false);
});

test("failed deletion preserves the card filters and expansion", async () => {
  const {panel} = setup();
  filters(panel);
  panel._hass.callWS = async () => { throw new Error("Delete failed"); };
  const action = click(panel, {deleteCampaign: "test-campaign"});
  await action.run();
  assert.equal(panel.$("#notice").textContent, "Delete failed");
  assert.equal(panel.campaignQueries.get("test-campaign"), "filter");
  assert.equal(panel.expandedCampaigns.has("test-campaign"), true);
  assert.equal(action.button.disabled, false);
});

test("campaigns start collapsed, with pending and returned clients by default", () => {
  const {panel} = setup();
  panel.renderCampaigns();
  const html = panel.$("#campaign-list").innerHTML;
  assert.match(html, /pending device/);
  assert.match(html, /returned device/);
  assert.doesNotMatch(html, /confirmed device|excluded device/);
  assert.match(html, /value="pending-returned" selected/);
  assert.doesNotMatch(html, /data-campaign-details="test-campaign" open/);
  assert.match(html, /<\/details>\s*<progress class="campaign-progress" max="100" value="33.3"/);
});

test("state, query and expansion persist through campaign rendering", () => {
  const {panel} = setup();
  panel.campaignStates.set("test-campaign", "all");
  panel.campaignQueries.set("test-campaign", "confirmed");
  panel.onToggle({target: {dataset: {campaignDetails: "test-campaign"}, open: true, isConnected: true}});
  for (let i = 0; i < 2; i++) {
    panel.renderCampaigns();
    const html = panel.$("#campaign-list").innerHTML;
    assert.match(html, /confirmed device/);
    assert.doesNotMatch(html, /returned device|pending device|excluded device/);
    assert.match(html, /value="all" selected/);
    assert.match(html, /data-campaign-details="test-campaign" open/);
    assert.match(html, /progress class="campaign-progress" max="100" value="33.3"/);
  }
});

test("controller strings and exclusion notes are escaped before HTML insertion", () => {
  const {panel} = setup();
  const item = panel.data.campaigns[0];
  item.name = '<img src=x onerror="bad()">';
  item.clients[0].name = "<script>bad()</script>";
  item.clients[0].exclusion_note = '" onclick="bad()';
  panel.renderCampaigns();
  const html = panel.$("#campaign-list").innerHTML;
  assert.doesNotMatch(html, /<img|<script>/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&quot; onclick=&quot;bad\(\)/);
});

test("non-source confirmation shows away-from-source evidence", () => {
  const {panel} = setup();
  const item = panel.data.campaigns[0];
  item.clients[2].first_away_seen = "2026-10-15T12:00:00Z";
  panel.campaignStates.set(item.id, "confirmed");
  assert.match(panel.renderCampaignRows(item), /Away from source/);
});

test("automatic refresh updates health without interrupting open forms", async () => {
  const {panel, calls} = setup();
  panel.$("#campaign-dialog").open = true;
  panel.$("#campaign-list").innerHTML = "Draft form must survive";
  panel.renderHealth = () => calls.push(["health"]);
  panel.renderSnapshot = () => calls.push(["render"]);
  await panel.refresh();
  assert.equal(panel.pendingRender, true);
  assert.equal(panel.$("#campaign-list").innerHTML, "Draft form must survive");
  assert.deepEqual(calls, [["ws", {type: "aruba_monitor/snapshot"}], ["health"]]);
  panel.$("#campaign-dialog").open = false;
  panel.renderPending();
  assert.deepEqual(calls.at(-1), ["render"]);
});

test("hass setter does not duplicate timers and disconnect cleans up", async () => {
  const {panel, intervals} = setup();
  let refreshes = 0;
  panel.refresh = async () => { refreshes++; };
  panel.hass = panel._hass;
  panel.hass = panel._hass;
  panel.connectedCallback();
  assert.equal(intervals.size, 1);
  assert.equal([...intervals.values()][0].ms, 5000);
  assert.equal(refreshes, 1);
  panel.disconnectedCallback();
  assert.equal(intervals.size, 0);
  panel.hass = panel._hass;
  assert.equal(intervals.size, 0);
  panel.connectedCallback();
  assert.equal(intervals.size, 1);
  assert.equal(refreshes, 2);
});

test("a response arriving after disconnect does not render stale observations", async () => {
  const {panel} = setup();
  let resolve;
  panel._hass.callWS = () => new Promise(done => { resolve = done; });
  let renders = 0;
  panel.renderSnapshot = () => { renders++; };
  const request = panel.refresh();
  await Promise.resolve();
  panel.disconnectedCallback();
  resolve(snapshot());
  await request;
  assert.equal(renders, 0);
  assert.equal(panel.inFlight, null);
});

test("concurrent automatic refreshes share one snapshot request", async () => {
  const {panel, calls} = setup();
  const first = panel.refresh();
  assert.equal(panel.refresh(), first);
  await first;
  assert.equal(calls.filter(item => item[0] === "ws").length, 1);
});

test("snapshot refresh never starts polling for non-admin accounts", async () => {
  const {panel, calls, intervals} = setup();
  panel.hass = {...panel._hass, user: {is_admin: false}};
  await panel.refresh();
  assert.equal(calls.length, 0);
  assert.equal(intervals.size, 0);
  assert.match(panel.$("#notice").textContent, /administrator/);
});

test("mutation payloads preserve campaign, device and MAC fields", async () => {
  const {panel, calls} = setup();
  panel.refresh = async force => calls.push(["refresh", force]);
  await click(panel, {exclude: "device-0", campaign: "test-campaign", excluded: "false"}).run();
  assert.deepEqual(calls[0], ["ws", {type: "aruba_monitor/campaign_exclude", campaign_id: "test-campaign", device_id: "device-0", excluded: true, note: "A verified test exclusion"}]);
  const form = {id: "campaign-form", fields: {name: "Rotation", source: " Old ", target: "New"}, querySelector: () => null, reset() {}};
  await panel.onSubmit({target: form, preventDefault() {}});
  assert.deepEqual(calls[2], ["ws", {type: "aruba_monitor/campaign_create", name: "Rotation", source: " Old ", target: "New"}]);
});

test("snapshot rendering retains content and horizontal scroll positions", () => {
  const {panel, root} = setup();
  panel.$("main").scrollTop = 123;
  const table = {dataset: {scrollKey: "clients"}, scrollLeft: 84};
  root.querySelectorAll = () => [table];
  panel.renderClients = () => { table.scrollLeft = 0; panel.$("main").scrollTop = 0; };
  panel.renderSnapshot();
  assert.equal(panel.$("main").scrollTop, 123);
  assert.equal(table.scrollLeft, 84);
});

test("device labels, MAC linking and unlinking use the original device identity", async () => {
  const {panel, calls} = setup();
  panel.openDevice = "device-0";
  panel.refresh = async force => calls.push(["refresh", force]);
  panel.renderDevice = (id, preserveDraft = false) => calls.push(["device", id, preserveDraft]);
  for (const form of [
    {id: "label-form", fields: {label: "Kitchen speaker"}},
    {id: "alias-form", fields: {mac: "aa:bb:cc:00:00:01"}},
  ]) await panel.onSubmit({target: {...form, querySelector: () => null}, preventDefault() {}});
  await click(panel, {unlink: "aa:bb:cc:00:00:02"}).run();
  assert.deepEqual(calls, [
    ["ws", {type: "aruba_monitor/client_label", device_id: "device-0", label: "Kitchen speaker"}],
    ["refresh", true], ["device", "device-0", false],
    ["ws", {type: "aruba_monitor/client_link", device_id: "device-0", mac: "aa:bb:cc:00:00:01"}],
    ["refresh", true], ["device", "device-0", true],
    ["ws", {type: "aruba_monitor/client_unlink", device_id: "device-0", mac: "aa:bb:cc:00:00:02"}],
    ["refresh", true], ["device", "device-0", true],
  ]);
});

test("failed form writes leave the draft open and show an error", async () => {
  const {panel} = setup();
  panel.$("#campaign-dialog").open = true;
  panel._hass.callWS = async () => { throw new Error("A source and target must differ"); };
  let resets = 0;
  const button = {disabled: false};
  const form = {id: "campaign-form", fields: {name: "Rotation", source: "Old", target: "Old"}, querySelector: () => button, reset: () => { resets++; }};
  await panel.onSubmit({target: form, preventDefault() {}});
  assert.equal(panel.$("#campaign-dialog").open, true);
  assert.match(panel.$("#campaign-error").textContent, /must differ/);
  assert.equal(resets, 0);
  assert.equal(button.disabled, false);
});

test("the panel waits for a hass connection before starting its refresh timer", () => {
  const {panel, intervals} = setup();
  panel._hass = undefined;
  panel.connectedCallback();
  assert.equal(intervals.size, 0);
});

test("synchronous connection failures do not leave refresh permanently in flight", async () => {
  const {panel} = setup();
  panel._hass.callWS = () => { throw new Error("Connection unavailable"); };
  await panel.refresh();
  assert.equal(panel.inFlight, null);
  assert.match(panel.$("#notice").textContent, /Connection unavailable/);
  panel._hass.callWS = async () => snapshot();
  await panel.refresh();
  assert.equal(panel.$("#notice").hidden, true);
});

test("clients open first, network follows, and migrations is last", () => {
  const {panel, root} = setup();
  assert.equal(panel.view, "clients");
  const tabs = Array.from(root.innerHTML.matchAll(/<button data-view="([^"]+)" aria-selected="([^"]+)"/g), match => [match[1], match[2]]);
  assert.deepEqual(tabs, [["clients", "true"], ["network", "false"], ["migrations", "false"]]);
  assert.match(root.innerHTML, /<section id="clients" class="view">/);
  assert.match(root.innerHTML, /<section id="migrations" class="view" hidden>/);
});

function haDevices(name = "Kitchen speaker") {
  return {
    devices: [{id: "aruba-device", name, macs: ["aa:bb:cc:00:00:00"], integration: "aruba_monitor"}],
    linked_devices: [{id: "speaker-device", name: "Speaker integration", macs: ["aa:bb:cc:00:00:00"], integration: "speaker"}],
  };
}

test("opening a client offers HA device and linked device navigation with escaped names", async () => {
  const {panel, calls} = setup();
  panel._hass.callWS = async message => {
    calls.push(["ws", JSON.parse(JSON.stringify(message))]);
    const result = haDevices('<img src=x onerror="bad()">');
    result.linked_devices[0].id = 'id/with"quotes';
    return result;
  };
  await click(panel, {device: "device-0"}).run();
  assert.equal(panel.$("#device-dialog").open, true);
  assert.deepEqual(calls, [["ws", {type: "aruba_monitor/client_devices", device_id: "device-0"}]]);
  const html = panel.$("#device-ha-links").innerHTML;
  assert.match(html, /href="\/config\/devices\/device\/aruba-device"/);
  assert.match(html, /href="\/config\/devices\/device\/id%2Fwith%22quotes"/);
  assert.match(html, /Linked devices/);
  assert.match(html, /Speaker integration/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  assert.equal((html.match(/Open device/g) || []).length, 2);
});

test("an absent registry device and no matching links have clear empty states", async () => {
  const {panel} = setup();
  panel._hass.callWS = async () => ({devices: [], linked_devices: []});
  await click(panel, {device: "device-0"}).run();
  const html = panel.$("#device-ha-links").innerHTML;
  assert.match(html, /not registered yet/);
  assert.match(html, /No linked Home Assistant devices found/);
  assert.doesNotMatch(html, /href=/);
});

test("device lookup failure can be retried without replacing client form drafts", async () => {
  const {panel} = setup();
  panel._hass.callWS = async () => { throw new Error("Disconnected"); };
  await click(panel, {device: "device-0"}).run();
  assert.match(panel.$("#device-ha-links").innerHTML, /Could not load/);
  panel.$("#device-label").value = "Unsaved label";
  panel.$("#alias-mac").value = "aa:bb:cc:00:00:02";
  panel._hass.callWS = async () => haDevices();
  await click(panel, {retryDeviceLinks: "true"}).run();
  assert.match(panel.$("#device-ha-links").innerHTML, /Open device/);
  assert.equal(panel.$("#device-label").value, "Unsaved label");
  assert.equal(panel.$("#alias-mac").value, "aa:bb:cc:00:00:02");
});

test("older device lookups cannot overwrite another client or a closed or unmounted dialog", async () => {
  const {panel} = setup();
  const pending = [];
  panel._hass.callWS = () => new Promise(resolve => pending.push(resolve));
  const first = click(panel, {device: "device-0"}).run();
  const second = click(panel, {device: "device-1"}).run();
  await Promise.resolve();
  pending[1](haDevices("Current client"));
  await second;
  pending[0](haDevices("Wrong client"));
  await first;
  assert.match(panel.$("#device-ha-links").innerHTML, /Current client/);
  assert.doesNotMatch(panel.$("#device-ha-links").innerHTML, /Wrong client/);
  const third = panel.loadDeviceLinks("device-1");
  panel.$("#device-dialog").close();
  await Promise.resolve();
  pending[2](haDevices("Closed client"));
  await third;
  assert.doesNotMatch(panel.$("#device-ha-links").innerHTML, /Closed client/);
  panel.$("#device-dialog").showModal();
  const fourth = panel.loadDeviceLinks("device-1");
  panel.disconnectedCallback();
  await Promise.resolve();
  pending[3](haDevices("Unmounted client"));
  await fourth;
  assert.doesNotMatch(panel.$("#device-ha-links").innerHTML, /Unmounted client/);
});

test("five-second refresh preserves client links, forms and dialog scroll", async () => {
  const {panel} = setup();
  panel._hass.callWS = async () => haDevices();
  await click(panel, {device: "device-0"}).run();
  const links = panel.$("#device-ha-links").innerHTML;
  panel.$("#device-label").value = "Draft label";
  panel.$("#device-dialog").scrollTop = 125;
  panel._hass.callWS = async () => snapshot();
  await panel.refresh();
  assert.equal(panel.$("#device-ha-links").innerHTML, links);
  assert.equal(panel.$("#device-label").value, "Draft label");
  assert.equal(panel.$("#device-dialog").scrollTop, 125);
});


test("a synchronous HA connection error leaves a retryable device lookup", async () => {
  const {panel} = setup();
  panel._hass.callWS = () => { throw new Error("Connection unavailable"); };
  await click(panel, {device: "device-0"}).run();
  assert.equal(panel.$("#device-dialog").open, true);
  assert.match(panel.$("#device-ha-links").innerHTML, /Could not load/);
  assert.match(panel.$("#device-ha-links").innerHTML, /Retry/);
});
