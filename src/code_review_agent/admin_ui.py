"""Dependency-free organization administration console resources."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse


_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


_ADMIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Review Admin</title>
  <link rel="stylesheet" href="/admin/assets/app.css">
  <script src="/admin/assets/app.js" defer></script>
</head>
<body>
  <header class="topbar">
    <a class="brand" href="/admin" aria-label="Review Admin home">
      <span class="brand-mark" aria-hidden="true">CR</span>
      <span>Review Admin</span>
    </a>
    <div class="topbar-actions">
      <span id="identity" class="identity" hidden></span>
      <button id="disconnect" class="button button-quiet" type="button" hidden>
        Disconnect
      </button>
    </div>
  </header>

  <main class="page">
    <section id="login-panel" class="panel login-panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Protected workspace</p>
          <h1>Administration</h1>
        </div>
        <span class="status-dot" aria-hidden="true"></span>
      </div>
      <form id="login-form" class="login-form">
        <label for="token-input">Access token</label>
        <div class="input-row">
          <input id="token-input" name="token" type="password" autocomplete="off"
                 spellcheck="false" required>
          <button class="button button-primary" type="submit">Connect</button>
        </div>
        <p id="login-status" class="form-status" role="status"></p>
      </form>
    </section>

    <section id="app-shell" hidden>
      <div id="flash" class="flash" role="alert" hidden></div>
      <div class="workspace-heading">
        <div>
          <p class="eyebrow">Organization workspace</p>
          <h1 id="workspace-title">Administration</h1>
        </div>
        <button id="refresh" class="button button-quiet" type="button">Refresh</button>
      </div>

      <nav class="tabs" aria-label="Administration views">
        <button class="tab is-active" type="button" data-view="approvals">Approvals</button>
        <button class="tab" type="button" data-view="repositories" data-role="org_admin">
          Repositories
        </button>
        <button class="tab" type="button" data-view="policy" data-role="org_admin">
          Policy
        </button>
        <button class="tab" type="button" data-view="members" data-role="org_admin">
          Members
        </button>
        <button class="tab" type="button" data-view="audit" data-role="org_admin">
          Audit
        </button>
      </nav>

      <section class="view" data-view-panel="approvals">
        <div class="section-heading">
          <div>
            <p class="eyebrow">Maintainer queue</p>
            <h2>Pending approvals</h2>
          </div>
          <span id="approval-count" class="count">0</span>
        </div>
        <div id="approval-list" class="stack"></div>
      </section>

      <section class="view" data-view-panel="repositories" hidden>
        <div class="section-heading">
          <div>
            <p class="eyebrow">Organization configuration</p>
            <h2>Repositories</h2>
          </div>
        </div>
        <form id="repository-form" class="panel form-grid">
          <h3>Register repository</h3>
          <label>Repository alias<input name="repository" required placeholder="owner/repository"></label>
          <label>Mode<select name="mode"><option value="shadow">Shadow</option><option value="guarded_publish">Guarded publish</option></select></label>
          <label>Budget (micro-USD)<input name="budget_microusd" type="number" min="0"></label>
          <label>Policy version<input name="policy_version" value="rbac/v1" required></label>
          <div class="form-actions"><button class="button button-primary" type="submit">Register</button></div>
        </form>
        <div id="repository-list" class="table-wrap"></div>
      </section>

      <section class="view" data-view-panel="policy" hidden>
        <div class="section-heading">
          <div>
            <p class="eyebrow">Organization configuration</p>
            <h2>Policy</h2>
          </div>
        </div>
        <form id="policy-form" class="panel form-grid">
          <label>Version<input name="version" required></label>
          <label>Severity levels<input name="severity_levels" placeholder="low, medium, high" required></label>
          <label>Forbidden operations<input name="forbidden_operations" placeholder="shell, network"></label>
          <label>Allowed tools<input name="allowed_tools" placeholder="read_file, search_repo"></label>
          <label>Approval threshold<input name="approval_threshold" type="number" min="1" max="100" required></label>
          <label>Retention days<input name="retention_days" type="number" min="1" max="3650" required></label>
          <label>Cost budget (micro-USD)<input name="cost_budget_microusd" type="number" min="0" required></label>
          <label>Source SHA<input name="source_sha" pattern="[0-9a-fA-F]{7,64}" required></label>
          <label class="span-2">Reason<textarea name="reason" maxlength="512" required></textarea></label>
          <div class="form-actions span-2"><button class="button button-primary" type="submit">Save policy</button></div>
        </form>
      </section>

      <section class="view" data-view-panel="members" hidden>
        <div class="section-heading">
          <div>
            <p class="eyebrow">Organization configuration</p>
            <h2>Members</h2>
          </div>
        </div>
        <form id="member-form" class="panel form-grid">
          <h3>Add member</h3>
          <label>Subject<input name="subject" required placeholder="oidc-subject"></label>
          <label>Display name<input name="display_name" required></label>
          <label>Role<select name="role"><option value="viewer">Viewer</option><option value="reviewer">Reviewer</option><option value="maintainer">Maintainer</option><option value="org_admin">Organization admin</option></select></label>
          <label>Repository IDs<input name="repository_ids" placeholder="Comma-separated IDs"></label>
          <div class="form-actions"><button class="button button-primary" type="submit">Add member</button></div>
        </form>
        <div id="member-list" class="table-wrap"></div>
      </section>

      <section class="view" data-view-panel="audit" hidden>
        <div class="section-heading">
          <div>
            <p class="eyebrow">Organization history</p>
            <h2>Audit events</h2>
          </div>
        </div>
        <div id="audit-list" class="table-wrap"></div>
      </section>
    </section>
  </main>
</body>
</html>
"""


_ADMIN_CSS = """/* Review Admin uses a deliberately small, local-only visual system. */
:root {
  color-scheme: light;
  --ink: #17212b;
  --muted: #66717c;
  --line: #d8e0e6;
  --surface: #ffffff;
  --wash: #f4f7f8;
  --teal: #087f8c;
  --teal-dark: #075d67;
  --amber: #a76308;
  --red: #a43d42;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

* { box-sizing: border-box; }
body { margin: 0; background: var(--wash); color: var(--ink); }
button, input, select, textarea { font: inherit; }
button { cursor: pointer; }
[hidden] { display: none !important; }

.topbar { min-height: 64px; display: flex; align-items: center; justify-content: space-between;
  padding: 0 32px; background: var(--surface); border-bottom: 1px solid var(--line); }
.brand { color: var(--ink); display: inline-flex; align-items: center; gap: 10px; font-weight: 750;
  letter-spacing: .01em; text-decoration: none; }
.brand-mark { display: grid; place-items: center; width: 30px; height: 30px; background: var(--teal);
  color: white; font-size: 11px; font-weight: 800; }
.topbar-actions { display: flex; align-items: center; gap: 14px; }
.identity { color: var(--muted); font-size: 13px; }
.page { width: min(1180px, calc(100% - 40px)); margin: 0 auto; padding: 44px 0 72px; }
.login-panel { width: min(620px, 100%); margin: 8vh auto 0; }
.workspace-heading, .section-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; }
.workspace-heading { margin-bottom: 28px; }
.eyebrow { margin: 0 0 8px; color: var(--teal); font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
h1, h2, h3, p { margin-top: 0; }
h1 { margin-bottom: 0; font-size: clamp(28px, 4vw, 42px); line-height: 1.05; letter-spacing: 0; }
h2 { margin-bottom: 0; font-size: 25px; letter-spacing: 0; }
h3 { margin-bottom: 18px; font-size: 16px; }
.panel, .approval-item { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; }
.panel { padding: 24px; }
.section-heading { margin-bottom: 20px; }
.status-dot { width: 10px; height: 10px; margin-top: 8px; background: var(--amber); border-radius: 50%; }
.input-row { display: flex; gap: 10px; }
label { display: grid; gap: 7px; color: var(--muted); font-size: 13px; font-weight: 650; }
input, select, textarea { width: 100%; border: 1px solid #b9c5cc; border-radius: 4px; background: white; color: var(--ink); padding: 10px 11px; }
input:focus, select:focus, textarea:focus, button:focus-visible { outline: 3px solid rgba(8, 127, 140, .2); outline-offset: 1px; border-color: var(--teal); }
textarea { min-height: 88px; resize: vertical; }
.button { border: 1px solid transparent; border-radius: 4px; padding: 9px 14px; font-weight: 750; white-space: nowrap; }
.button-primary { background: var(--teal); color: white; }
.button-primary:hover { background: var(--teal-dark); }
.button-quiet { background: white; color: var(--ink); border-color: var(--line); }
.button-danger { background: white; color: var(--red); border-color: #dcaeb1; }
.button:disabled { cursor: wait; opacity: .55; }
.form-status { min-height: 20px; margin: 10px 0 0; color: var(--muted); font-size: 13px; }
.form-status.error, .flash.error { color: var(--red); }
.flash { margin-bottom: 18px; padding: 12px 14px; border: 1px solid var(--line); border-left: 4px solid var(--teal); background: white; font-size: 14px; }
.flash.error { border-left-color: var(--red); }
.tabs { display: flex; gap: 6px; overflow-x: auto; border-bottom: 1px solid var(--line); margin-bottom: 30px; }
.tab { border: 0; border-bottom: 3px solid transparent; padding: 11px 14px; background: transparent; color: var(--muted); font-weight: 720; white-space: nowrap; }
.tab.is-active { border-bottom-color: var(--teal); color: var(--ink); }
.count { min-width: 32px; padding: 5px 9px; border-radius: 20px; background: #e5f1f2; color: var(--teal-dark); font-size: 13px; text-align: center; }
.stack { display: grid; gap: 12px; }
.approval-item { padding: 18px; }
.approval-meta { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
.approval-title { margin: 0; font-size: 16px; }
.approval-subtitle, .muted { color: var(--muted); font-size: 13px; }
.approval-details { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 18px 0; }
.detail { padding: 10px; background: var(--wash); }
.detail dt { color: var(--muted); font-size: 11px; text-transform: uppercase; }
.detail dd { margin: 5px 0 0; overflow-wrap: anywhere; font-size: 13px; }
.approval-actions, .form-actions { display: flex; gap: 8px; justify-content: flex-end; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-bottom: 22px; }
.form-grid h3, .span-2 { grid-column: 1 / -1; }
.table-wrap { overflow-x: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 6px; }
table { width: 100%; border-collapse: collapse; min-width: 720px; }
th, td { padding: 12px 14px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle; font-size: 13px; }
th { background: #edf2f3; color: var(--muted); font-size: 11px; letter-spacing: .04em; text-transform: uppercase; }
tr:last-child td { border-bottom: 0; }
td input, td select { min-width: 120px; padding: 7px 8px; }
.empty { padding: 28px; border: 1px dashed #b9c5cc; color: var(--muted); text-align: center; }
.error-text { color: var(--red); }

@media (max-width: 700px) {
  .topbar { padding: 0 18px; }
  .page { width: min(100% - 28px, 1180px); padding-top: 28px; }
  .identity { display: none; }
  .input-row, .workspace-heading, .section-heading, .approval-meta { flex-direction: column; align-items: stretch; }
  .input-row .button { width: 100%; }
  .form-grid, .approval-details { grid-template-columns: 1fr; }
  .form-grid h3, .span-2 { grid-column: auto; }
  .approval-actions, .form-actions { justify-content: stretch; }
  .approval-actions .button, .form-actions .button { flex: 1; }
}
"""


_ADMIN_JS = """(() => {
  "use strict";

  const TOKEN_KEY = "crag.admin.token";
  const state = {
    token: sessionStorage.getItem(TOKEN_KEY) || "",
    principal: null,
    view: "approvals",
    approvals: [],
    repositories: [],
    members: [],
  };

  const byId = (id) => document.getElementById(id);
  const all = (selector) => Array.from(document.querySelectorAll(selector));
  const role = () => state.principal?.role || "";
  const isAdmin = () => role() === "org_admin";
  const canApprove = () => role() === "org_admin" || role() === "maintainer";

  function setStatus(message, error = false) {
    const node = byId("login-status");
    node.textContent = message || "";
    node.classList.toggle("error", error);
  }

  function flash(message, error = false) {
    const node = byId("flash");
    node.hidden = !message;
    node.textContent = message || "";
    node.classList.toggle("error", error);
  }

  function apiError(data, response) {
    return data?.error?.code || `request_failed_${response.status}`;
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
    if (options.body && typeof options.body !== "string") {
      headers.set("Content-Type", "application/json");
      options.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    if (!response.ok) throw new Error(apiError(data, response));
    return data;
  }

  function showView(view) {
    const target = document.querySelector(`[data-view-panel="${view}"]`);
    const tab = document.querySelector(`[data-view="${view}"]`);
    if (!target || !tab || tab.hidden) return;
    state.view = view;
    all("[data-view-panel]").forEach((node) => { node.hidden = node !== target; });
    all("[data-view]").forEach((node) => node.classList.toggle("is-active", node.dataset.view === view));
    loadView(view).catch((error) => flash(error.message, true));
  }

  function applyRoleGates() {
    all("[data-role]").forEach((node) => {
      node.hidden = node.dataset.role === "org_admin" && !isAdmin();
    });
    const approvalsTab = document.querySelector('[data-view="approvals"]');
    approvalsTab.hidden = !canApprove();
    if (!canApprove()) {
      showView("approvals");
      byId("approval-list").textContent = "";
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No approval access for this role.";
      byId("approval-list").append(empty);
    }
  }

  function renderIdentity() {
    const principal = state.principal;
    byId("identity").textContent = `${principal.user_id} / ${principal.role}`;
    byId("identity").hidden = false;
    byId("workspace-title").textContent = principal.organization_id;
  }

  function showConnected() {
    byId("login-panel").hidden = true;
    byId("app-shell").hidden = false;
    byId("disconnect").hidden = false;
    renderIdentity();
    applyRoleGates();
    showView(canApprove() ? "approvals" : "repositories");
  }

  function disconnect() {
    sessionStorage.removeItem(TOKEN_KEY);
    state.token = "";
    state.principal = null;
    byId("app-shell").hidden = true;
    byId("disconnect").hidden = true;
    byId("identity").hidden = true;
    byId("login-panel").hidden = false;
    byId("token-input").value = "";
    setStatus("Disconnected");
  }

  async function connect(event) {
    if (event) event.preventDefault();
    const input = byId("token-input");
    const token = input.value.trim() || state.token;
    if (!token) return setStatus("Access token is required", true);
    state.token = token;
    sessionStorage.setItem(TOKEN_KEY, token);
    setStatus("Connecting...");
    try {
      state.principal = await api("/v1/principal");
      setStatus("");
      showConnected();
    } catch (error) {
      sessionStorage.removeItem(TOKEN_KEY);
      state.token = "";
      state.principal = null;
      setStatus(error.message, true);
    }
  }

  function empty(node, message) {
    node.textContent = "";
    const item = document.createElement("p");
    item.className = "empty";
    item.textContent = message;
    node.append(item);
  }

  function valueOrBlank(value) { return value == null ? "" : String(value); }

  function detail(label, value) {
    const wrapper = document.createElement("div");
    wrapper.className = "detail";
    const term = document.createElement("dt");
    term.textContent = label;
    const definition = document.createElement("dd");
    definition.textContent = valueOrBlank(value);
    wrapper.append(term, definition);
    return wrapper;
  }

  function renderApprovals() {
    const list = byId("approval-list");
    list.textContent = "";
    byId("approval-count").textContent = String(state.approvals.length);
    if (!state.approvals.length) return empty(list, "No pending approvals.");
    state.approvals.forEach((record, index) => {
      const item = document.createElement("article");
      item.className = "approval-item";
      const meta = document.createElement("div");
      meta.className = "approval-meta";
      const heading = document.createElement("div");
      const title = document.createElement("h3");
      title.className = "approval-title";
      title.textContent = `${record.repository} / ${record.pull_request}`;
      const subtitle = document.createElement("p");
      subtitle.className = "approval-subtitle";
      subtitle.textContent = `Review ${record.review_job_id} · expires ${record.expires_at}`;
      heading.append(title, subtitle);
      meta.append(heading);
      const details = document.createElement("dl");
      details.className = "approval-details";
      details.append(
        detail("Head SHA", record.head_sha),
        detail("Payload SHA", record.payload_sha256),
        detail("Policy", record.policy_version),
      );
      const actions = document.createElement("div");
      actions.className = "approval-actions";
      ["approved", "rejected"].forEach((decision) => {
        const button = document.createElement("button");
        button.className = decision === "rejected" ? "button button-danger" : "button button-primary";
        button.type = "button";
        button.dataset.approvalIndex = String(index);
        button.dataset.decision = decision;
        button.textContent = decision === "approved" ? "Approve" : "Reject";
        actions.append(button);
      });
      item.append(meta, details, actions);
      list.append(item);
    });
  }

  function inputCell(value, name, type = "text") {
    const input = document.createElement("input");
    input.name = name;
    input.type = type;
    input.value = valueOrBlank(value);
    return input;
  }

  function renderRepositories() {
    const node = byId("repository-list");
    node.textContent = "";
    if (!state.repositories.length) return empty(node, "No repositories registered.");
    const table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>Alias</th><th>Mode</th><th>Budget</th><th>Policy</th><th>Action</th></tr></thead>";
    const body = document.createElement("tbody");
    state.repositories.forEach((record, index) => {
      const row = document.createElement("tr");
      const alias = document.createElement("td");
      alias.textContent = record.repository || record.alias || "";
      const mode = document.createElement("td");
      const modeInput = document.createElement("select");
      modeInput.name = "mode";
      ["shadow", "guarded_publish"].forEach((optionValue) => {
        const option = document.createElement("option");
        option.value = optionValue;
        option.textContent = optionValue;
        option.selected = record.mode === optionValue;
        modeInput.append(option);
      });
      mode.append(modeInput);
      const budget = document.createElement("td");
      budget.append(inputCell(record.budget_microusd, "budget_microusd", "number"));
      const policy = document.createElement("td");
      policy.append(inputCell(record.policy_version, "policy_version"));
      const action = document.createElement("td");
      const save = document.createElement("button");
      save.className = "button button-quiet";
      save.type = "button";
      save.dataset.repositoryIndex = String(index);
      save.textContent = "Save";
      action.append(save);
      row.append(alias, mode, budget, policy, action);
      body.append(row);
    });
    table.append(body);
    node.append(table);
  }

  function renderPolicy(record) {
    const form = byId("policy-form");
    if (!record) {
      form.reset();
      flash("No active policy");
      return;
    }
    const set = (name, value) => { form.elements[name].value = valueOrBlank(value); };
    set("version", record.version);
    set("severity_levels", (record.severity_levels || []).join(", "));
    set("forbidden_operations", (record.forbidden_operations || []).join(", "));
    set("allowed_tools", (record.allowed_tools || []).join(", "));
    set("approval_threshold", record.approval_threshold);
    set("retention_days", record.retention_days);
    set("cost_budget_microusd", record.cost_budget_microusd);
    set("source_sha", record.source_sha);
    set("reason", record.reason);
  }

  function renderMembers() {
    const node = byId("member-list");
    node.textContent = "";
    if (!state.members.length) return empty(node, "No members found.");
    const table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>Member</th><th>Subject</th><th>Role</th><th>Repositories</th><th>Action</th></tr></thead>";
    const body = document.createElement("tbody");
    state.members.forEach((record, index) => {
      const row = document.createElement("tr");
      const member = document.createElement("td");
      member.textContent = record.display_name || record.user_id || "";
      const subject = document.createElement("td");
      subject.textContent = record.subject || "";
      const roleCell = document.createElement("td");
      const roleInput = document.createElement("select");
      ["viewer", "reviewer", "maintainer", "org_admin"].forEach((optionValue) => {
        const option = document.createElement("option");
        option.value = optionValue;
        option.textContent = optionValue;
        option.selected = record.role === optionValue;
        roleInput.append(option);
      });
      roleCell.append(roleInput);
      const repositories = document.createElement("td");
      repositories.append(inputCell((record.repository_ids || []).join(","), "repository_ids"));
      const action = document.createElement("td");
      const save = document.createElement("button");
      save.className = "button button-quiet";
      save.type = "button";
      save.dataset.memberIndex = String(index);
      save.textContent = "Save";
      action.append(save);
      row.append(member, subject, roleCell, repositories, action);
      body.append(row);
    });
    table.append(body);
    node.append(table);
  }

  function renderAudit(records) {
    const node = byId("audit-list");
    node.textContent = "";
    if (!records.length) return empty(node, "No audit events.");
    const table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>Time</th><th>Action</th><th>Decision</th><th>Resource</th><th>Reason</th></tr></thead>";
    const body = document.createElement("tbody");
    records.forEach((record) => {
      const row = document.createElement("tr");
      ["occurred_at", "action", "decision", "resource_type", "reason_code"].forEach((key) => {
        const cell = document.createElement("td");
        cell.textContent = valueOrBlank(record[key]);
        row.append(cell);
      });
      body.append(row);
    });
    table.append(body);
    node.append(table);
  }

  function splitValues(value) {
    return value.split(",").map((item) => item.trim()).filter(Boolean);
  }

  async function loadView(view) {
    if (!state.principal) return;
    const org = encodeURIComponent(state.principal.organization_id);
    if (view === "approvals" && canApprove()) {
      state.approvals = (await api("/v1/reviews/pending-approval")).reviews || [];
      renderApprovals();
    } else if (view === "repositories" && isAdmin()) {
      state.repositories = (await api(`/v1/organizations/${org}/repositories`)).repositories || [];
      renderRepositories();
    } else if (view === "policy" && isAdmin()) {
      renderPolicy((await api(`/v1/organizations/${org}/policy`)).policy);
    } else if (view === "members" && isAdmin()) {
      state.members = (await api(`/v1/organizations/${org}/memberships`)).memberships || [];
      renderMembers();
    } else if (view === "audit" && isAdmin()) {
      const result = await api("/v1/audit-events?limit=50");
      renderAudit(result.audit_events || []);
    }
  }

  async function submitRepository(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const value = (name) => form.elements[name].value.trim();
    const saved = await submitForm(() => api(`/v1/organizations/${encodeURIComponent(state.principal.organization_id)}/repositories`, {
      method: "POST",
      body: {
        repository: value("repository"),
        mode: value("mode"),
        budget_microusd: value("budget_microusd") ? Number(value("budget_microusd")) : null,
        policy_version: value("policy_version"),
      },
    }), "Repository registered");
    if (!saved) return;
    form.reset();
    await loadView("repositories");
  }

  async function saveRepository(button) {
    const record = state.repositories[Number(button.dataset.repositoryIndex)];
    const row = button.closest("tr");
    const value = (name) => row.querySelector(`[name="${name}"]`).value.trim();
    const saved = await submitForm(() => api(`/v1/organizations/${encodeURIComponent(state.principal.organization_id)}/repositories/${record.id}`, {
      method: "PATCH",
      body: {
        mode: value("mode"),
        budget_microusd: value("budget_microusd") ? Number(value("budget_microusd")) : null,
        policy_version: value("policy_version"),
      },
    }), "Repository updated");
    if (!saved) return;
    await loadView("repositories");
  }

  async function submitPolicy(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const value = (name) => form.elements[name].value.trim();
    const saved = await submitForm(() => api(`/v1/organizations/${encodeURIComponent(state.principal.organization_id)}/policy`, {
      method: "PUT",
      body: {
        version: value("version"), severity_levels: splitValues(value("severity_levels")),
        forbidden_operations: splitValues(value("forbidden_operations")),
        allowed_tools: splitValues(value("allowed_tools")),
        approval_threshold: Number(value("approval_threshold")), retention_days: Number(value("retention_days")),
        cost_budget_microusd: Number(value("cost_budget_microusd")), source_sha: value("source_sha"), reason: value("reason"),
      },
    }), "Policy saved");
    if (!saved) return;
    await loadView("policy");
  }

  async function submitMember(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const value = (name) => form.elements[name].value.trim();
    const saved = await submitForm(() => api(`/v1/organizations/${encodeURIComponent(state.principal.organization_id)}/memberships`, {
      method: "POST",
      body: { subject: value("subject"), display_name: value("display_name"), role: value("role"), repository_ids: splitValues(value("repository_ids")) },
    }), "Member added");
    if (!saved) return;
    form.reset();
    await loadView("members");
  }

  async function saveMember(button) {
    const record = state.members[Number(button.dataset.memberIndex)];
    const row = button.closest("tr");
    const roleValue = row.querySelector("select").value;
    const repositoryValue = row.querySelector("input").value;
    const saved = await submitForm(() => api(`/v1/organizations/${encodeURIComponent(state.principal.organization_id)}/memberships/${record.membership_id}`, {
      method: "PATCH", body: { role: roleValue, repository_ids: splitValues(repositoryValue) },
    }), "Member updated");
    if (!saved) return;
    await loadView("members");
  }

  async function decideApproval(button) {
    const record = state.approvals[Number(button.dataset.approvalIndex)];
    const decision = button.dataset.decision;
    if (!record || !window.confirm(`${decision === "approved" ? "Approve" : "Reject"} publication for ${record.repository} / ${record.pull_request}?`)) return;
    button.disabled = true;
    const saved = await submitForm(() => api(`/v1/reviews/${record.review_job_id}/${decision === "approved" ? "approve" : "reject"}`, {
      method: "POST", body: { payload_sha256: record.payload_sha256, nonce: record.nonce },
    }), decision === "approved" ? "Publication approved" : "Publication rejected");
    if (!saved) {
      button.disabled = false;
      return;
    }
    await loadView("approvals");
  }

  async function submitForm(operation, successMessage) {
    try { await operation(); flash(successMessage); return true; }
    catch (error) { flash(error.message, true); return false; }
  }

  function bindEvents() {
    byId("login-form").addEventListener("submit", connect);
    byId("disconnect").addEventListener("click", disconnect);
    byId("refresh").addEventListener("click", () => loadView(state.view).catch((error) => flash(error.message, true)));
    all("[data-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
    byId("repository-form").addEventListener("submit", (event) => submitRepository(event).catch((error) => flash(error.message, true)));
    byId("policy-form").addEventListener("submit", (event) => submitPolicy(event).catch((error) => flash(error.message, true)));
    byId("member-form").addEventListener("submit", (event) => submitMember(event).catch((error) => flash(error.message, true)));
    byId("approval-list").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-decision]");
      if (button) decideApproval(button).catch((error) => flash(error.message, true));
    });
    byId("repository-list").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-repository-index]");
      if (button) saveRepository(button).catch((error) => flash(error.message, true));
    });
    byId("member-list").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-member-index]");
      if (button) saveMember(button).catch((error) => flash(error.message, true));
    });
  }

  bindEvents();
  if (state.token) {
    connect();
  }
})();
"""


def install_admin_ui(app: FastAPI) -> None:
    """Install the same-origin administration shell and its static resources."""

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def admin_index() -> HTMLResponse:
        return HTMLResponse(_ADMIN_HTML, headers=_SECURITY_HEADERS)

    @app.get("/admin/assets/app.css", response_class=PlainTextResponse, include_in_schema=False)
    async def admin_styles() -> PlainTextResponse:
        return PlainTextResponse(_ADMIN_CSS, media_type="text/css", headers=_SECURITY_HEADERS)

    @app.get("/admin/assets/app.js", response_class=PlainTextResponse, include_in_schema=False)
    async def admin_script() -> PlainTextResponse:
        return PlainTextResponse(_ADMIN_JS, media_type="text/javascript", headers=_SECURITY_HEADERS)
