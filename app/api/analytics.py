from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.request_analytics import build_analytics_report


router = APIRouter(tags=["analytics"])


@router.get("/api/v1/analytics")
def get_analytics(
    request: Request,
    days: int = Query(default=7, ge=0, le=365),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    settings = request.app.state.settings
    return build_analytics_report(settings.db_path, days=days, limit=limit)


@router.get("/analytics", response_class=HTMLResponse)
def analytics_dashboard() -> HTMLResponse:
    return HTMLResponse(_analytics_dashboard_html())


def _analytics_dashboard_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Bus API Analytics</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4efe6;
      --bg-strong: radial-gradient(circle at top left, rgba(214, 121, 59, 0.24), transparent 34%),
        radial-gradient(circle at top right, rgba(26, 122, 110, 0.18), transparent 30%),
        linear-gradient(180deg, #faf7f1 0%, #f1eadf 100%);
      --panel: rgba(255, 252, 247, 0.86);
      --panel-border: rgba(84, 63, 45, 0.12);
      --text: #2d241d;
      --muted: #6f6256;
      --accent: #c35c2b;
      --accent-soft: rgba(195, 92, 43, 0.12);
      --secondary: #1d7668;
      --secondary-soft: rgba(29, 118, 104, 0.14);
      --shadow: 0 22px 48px rgba(64, 42, 26, 0.12);
      --radius: 22px;
      --radius-sm: 14px;
      --font-sans: "IBM Plex Sans", "Segoe UI Variable Text", system-ui, sans-serif;
      --font-mono: "IBM Plex Mono", "Cascadia Code", ui-monospace, monospace;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg-strong);
      color: var(--text);
      font-family: var(--font-sans);
      line-height: 1.5;
    }

    main {
      width: min(1200px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }

    .hero {
      padding: 28px;
      border: 1px solid var(--panel-border);
      border-radius: calc(var(--radius) + 6px);
      background: linear-gradient(135deg, rgba(255, 248, 239, 0.92), rgba(249, 244, 236, 0.78));
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }

    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 12px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    h1 {
      margin: 14px 0 10px;
      font-size: clamp(30px, 4vw, 52px);
      line-height: 0.98;
      letter-spacing: -0.04em;
    }

    .hero p {
      margin: 0;
      color: var(--muted);
      max-width: 760px;
      font-size: 15px;
    }

    .filters {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 14px;
      margin-top: 22px;
    }

    .field {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .field label {
      font-size: 13px;
      font-weight: 600;
      color: var(--muted);
    }

    .field input {
      width: 100%;
      padding: 12px 14px;
      border: 1px solid rgba(84, 63, 45, 0.16);
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.76);
      color: var(--text);
      font: inherit;
    }

    .actions {
      display: flex;
      align-items: end;
    }

    button {
      width: 100%;
      padding: 12px 16px;
      border: 0;
      border-radius: 14px;
      background: linear-gradient(135deg, var(--accent), #da7d4b);
      color: #fff8f2;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      transition: transform 140ms ease, box-shadow 140ms ease;
      box-shadow: 0 12px 30px rgba(195, 92, 43, 0.28);
    }

    button:hover {
      transform: translateY(-1px);
    }

    .statusline {
      display: flex;
      flex-wrap: wrap;
      gap: 10px 16px;
      margin-top: 16px;
      font-size: 13px;
      color: var(--muted);
    }

    .grid {
      display: grid;
      gap: 18px;
      margin-top: 22px;
    }

    .cards {
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    }

    .card,
    .panel {
      border: 1px solid var(--panel-border);
      border-radius: var(--radius);
      background: var(--panel);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }

    .card {
      padding: 20px;
    }

    .card h2 {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .metric {
      margin-top: 10px;
      font-size: clamp(28px, 3vw, 40px);
      font-weight: 700;
      letter-spacing: -0.05em;
    }

    .submetric {
      margin-top: 6px;
      font-size: 13px;
      color: var(--muted);
    }

    .split {
      grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
    }

    .panel {
      overflow: hidden;
    }

    .panel-head {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      padding: 20px 20px 0;
    }

    .panel-head h2 {
      margin: 0;
      font-size: 18px;
      letter-spacing: -0.03em;
    }

    .panel-head span {
      color: var(--muted);
      font-size: 13px;
    }

    .panel-body {
      padding: 16px 20px 20px;
    }

    .bars {
      display: grid;
      gap: 12px;
    }

    .bar-row {
      display: grid;
      gap: 6px;
    }

    .bar-meta {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-size: 13px;
    }

    .bar-track {
      height: 10px;
      border-radius: 999px;
      background: rgba(45, 36, 29, 0.08);
      overflow: hidden;
    }

    .bar-fill {
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--secondary), #4eaea0);
      transform-origin: left;
      animation: expand 420ms ease both;
    }

    @keyframes expand {
      from { transform: scaleX(0.2); opacity: 0.35; }
      to { transform: scaleX(1); opacity: 1; }
    }

    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }

    .chip {
      padding: 10px 12px;
      border-radius: 999px;
      background: var(--secondary-soft);
      color: var(--secondary);
      font-size: 13px;
      font-weight: 600;
    }

    .table-wrap {
      overflow-x: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }

    th,
    td {
      padding: 12px 10px;
      border-bottom: 1px solid rgba(84, 63, 45, 0.1);
      text-align: left;
      vertical-align: top;
    }

    th {
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    td code {
      font-family: var(--font-mono);
      font-size: 12px;
      color: var(--accent);
      word-break: break-word;
    }

    .muted {
      color: var(--muted);
    }

    .empty,
    .error {
      padding: 18px;
      border-radius: var(--radius-sm);
      font-size: 14px;
    }

    .empty {
      background: rgba(29, 118, 104, 0.08);
      color: var(--secondary);
    }

    .error {
      background: rgba(195, 92, 43, 0.1);
      color: var(--accent);
    }

    @media (max-width: 900px) {
      main {
        width: min(100vw - 20px, 1200px);
        padding-top: 20px;
      }

      .hero,
      .card,
      .panel-body {
        padding-left: 16px;
        padding-right: 16px;
      }

      .panel-head {
        padding-left: 16px;
        padding-right: 16px;
      }

      .split {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <div class="eyebrow">Read-only analytics</div>
      <h1>Bus API request analytics</h1>
      <p>Tracks app-style user agents like <code>YABus/version-commitHash (Platform)</code> and browser user agents separately, then breaks usage down by version, system, platform, and endpoint.</p>

      <form id="filters" class="filters">
        <div class="field">
          <label for="days">Days</label>
          <input id="days" name="days" type="number" min="0" max="365" value="7" />
        </div>
        <div class="field">
          <label for="limit">Rows per table</label>
          <input id="limit" name="limit" type="number" min="1" max="200" value="20" />
        </div>
        <div class="actions">
          <button type="submit">Refresh dashboard</button>
        </div>
      </form>

      <div class="statusline">
        <span id="period-label">Loading period...</span>
        <span id="generated-label">Preparing analytics...</span>
      </div>
    </section>

    <section id="summary-cards" class="grid cards"></section>

    <section class="grid split">
      <article class="panel">
        <div class="panel-head">
          <h2>Requests by day</h2>
          <span id="daily-caption">Recent trend</span>
        </div>
        <div class="panel-body" id="daily-bars"></div>
      </article>

      <article class="panel">
        <div class="panel-head">
          <h2>Client mix</h2>
          <span>By detected family/platform</span>
        </div>
        <div class="panel-body">
          <div id="client-chips" class="chips"></div>
          <div id="platform-chips" class="chips" style="margin-top: 12px;"></div>
          <div id="system-chips" class="chips" style="margin-top: 12px;"></div>
        </div>
      </article>
    </section>

    <section class="grid">
      <article class="panel">
        <div class="panel-head">
          <h2>Top endpoints</h2>
          <span>Grouped by route template</span>
        </div>
        <div class="panel-body table-wrap" id="endpoint-table"></div>
      </article>

      <article class="panel">
        <div class="panel-head">
          <h2>App clients</h2>
          <span>YABus version, platform, system, endpoint</span>
        </div>
        <div class="panel-body table-wrap" id="app-table"></div>
      </article>

      <article class="panel">
        <div class="panel-head">
          <h2>Web clients</h2>
          <span>Browser version, platform, system, endpoint</span>
        </div>
        <div class="panel-body table-wrap" id="web-table"></div>
      </article>

      <article class="panel">
        <div class="panel-head">
          <h2>Recent requests</h2>
          <span>Last sampled hits</span>
        </div>
        <div class="panel-body table-wrap" id="recent-table"></div>
      </article>
    </section>
  </main>

  <script>
    const filtersForm = document.getElementById("filters");
    const daysInput = document.getElementById("days");
    const limitInput = document.getElementById("limit");

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function formatNumber(value) {
      return new Intl.NumberFormat().format(Number(value ?? 0));
    }

    function formatTime(unixSeconds) {
      if (!unixSeconds) {
        return "n/a";
      }
      return new Date(Number(unixSeconds) * 1000).toLocaleString();
    }

    function readFiltersFromUrl() {
      const params = new URLSearchParams(window.location.search);
      const days = params.get("days") ?? "7";
      const limit = params.get("limit") ?? "20";
      daysInput.value = days;
      limitInput.value = limit;
      return { days, limit };
    }

    function writeFiltersToUrl(days, limit) {
      const url = new URL(window.location.href);
      url.searchParams.set("days", days);
      url.searchParams.set("limit", limit);
      window.history.replaceState({}, "", url);
    }

    function renderCards(summary) {
      const cards = [
        ["Total requests", formatNumber(summary.total_requests), "Across the selected period"],
        ["Endpoints", formatNumber(summary.unique_endpoints), "Unique route templates seen"],
        ["User agents", formatNumber(summary.unique_user_agents), "Distinct raw user-agent strings"],
        ["App versions", formatNumber(summary.unique_app_versions), "Custom YABus versions detected"],
        ["Browsers", formatNumber(summary.unique_browsers), "Distinct browser families detected"],
        ["Last seen", escapeHtml(formatTime(summary.last_seen_at)), "Most recent stored request"],
      ];

      document.getElementById("summary-cards").innerHTML = cards.map(([label, value, hint]) => `
        <article class="card">
          <h2>${escapeHtml(label)}</h2>
          <div class="metric">${value}</div>
          <div class="submetric">${escapeHtml(hint)}</div>
        </article>
      `).join("");
    }

    function renderBars(rows) {
      const target = document.getElementById("daily-bars");
      if (!rows.length) {
        target.innerHTML = '<div class="empty">No request data in this time window yet.</div>';
        return;
      }

      const maxCount = Math.max(...rows.map((row) => Number(row.request_count || 0)), 1);
      target.innerHTML = `
        <div class="bars">
          ${rows.map((row) => {
            const count = Number(row.request_count || 0);
            const width = Math.max(8, (count / maxCount) * 100);
            return `
              <div class="bar-row">
                <div class="bar-meta">
                  <span>${escapeHtml(row.date)}</span>
                  <strong>${formatNumber(count)}</strong>
                </div>
                <div class="bar-track">
                  <div class="bar-fill" style="width: ${width}%"></div>
                </div>
              </div>
            `;
          }).join("")}
        </div>
      `;
    }

    function renderChips(targetId, rows, labelKey, valueFormatter) {
      const target = document.getElementById(targetId);
      if (!rows.length) {
        target.innerHTML = '<div class="empty">No matching analytics rows yet.</div>';
        return;
      }

      target.innerHTML = rows.map((row) => `
        <div class="chip">${escapeHtml(valueFormatter(row[labelKey], row))}: ${formatNumber(row.request_count)}</div>
      `).join("");
    }

    function renderTable(targetId, columns, rows) {
      const target = document.getElementById(targetId);
      if (!rows.length) {
        target.innerHTML = '<div class="empty">No matching analytics rows yet.</div>';
        return;
      }

      const head = columns.map((column) => `<th>${escapeHtml(column.label)}</th>`).join("");
      const body = rows.map((row) => `
        <tr>
          ${columns.map((column) => `<td>${column.render(row)}</td>`).join("")}
        </tr>
      `).join("");

      target.innerHTML = `
        <table>
          <thead><tr>${head}</tr></thead>
          <tbody>${body}</tbody>
        </table>
      `;
    }

    function codeCell(value) {
      return `<code>${escapeHtml(value || "n/a")}</code>`;
    }

    function textCell(value) {
      return escapeHtml(value || "n/a");
    }

    function endpointCell(value) {
      return `<code>${escapeHtml(value || "n/a")}</code>`;
    }

    function detailCell(parts) {
      const filtered = parts.filter(Boolean);
      return filtered.length ? escapeHtml(filtered.join(" / ")) : '<span class="muted">n/a</span>';
    }

    async function loadDashboard() {
      const { days, limit } = readFiltersFromUrl();
      writeFiltersToUrl(days, limit);

      document.getElementById("period-label").textContent = Number(days) > 0
        ? `Showing the last ${days} day(s)`
        : "Showing all stored data";
      document.getElementById("generated-label").textContent = "Loading analytics...";
      document.getElementById("daily-caption").textContent = Number(days) > 0
        ? `${days} day window`
        : "All-time trend sample";

      try {
        const response = await fetch(`/api/v1/analytics?days=${encodeURIComponent(days)}&limit=${encodeURIComponent(limit)}`, {
          headers: {
            "Accept": "application/json",
          },
        });

        if (!response.ok) {
          throw new Error(`Analytics request failed with ${response.status}`);
        }

        const data = await response.json();
        document.getElementById("generated-label").textContent = `Generated at ${formatTime(data.generated_at)}`;

        renderCards(data.summary || {});
        renderBars(data.daily || []);
        renderChips("client-chips", data.client_types || [], "client_type", (value) => value || "unknown");
        renderChips("platform-chips", data.platforms || [], "platform", (value) => value || "unknown");
        renderChips("system-chips", data.systems || [], "system", (value, row) => [value, row.system_version].filter(Boolean).join(" ") || "unknown");

        renderTable("endpoint-table", [
          { label: "Endpoint", render: (row) => endpointCell(row.endpoint) },
          { label: "Requests", render: (row) => formatNumber(row.request_count) },
          { label: "App", render: (row) => formatNumber(row.app_requests) },
          { label: "Web", render: (row) => formatNumber(row.web_requests) },
          { label: "Unknown", render: (row) => formatNumber(row.unknown_requests) },
          { label: "Last seen", render: (row) => textCell(formatTime(row.last_seen_at)) },
        ], data.endpoints || []);

        renderTable("app-table", [
          { label: "Version", render: (row) => codeCell(row.app_version) },
          { label: "Commit", render: (row) => codeCell(row.app_commit_hash || "n/a") },
          { label: "Platform / System", render: (row) => detailCell([row.platform, row.system]) },
          { label: "Endpoint", render: (row) => endpointCell(row.endpoint) },
          { label: "Requests", render: (row) => formatNumber(row.request_count) },
          { label: "Last seen", render: (row) => textCell(formatTime(row.last_seen_at)) },
        ], data.app_usage || []);

        renderTable("web-table", [
          { label: "Browser", render: (row) => detailCell([row.browser_name, row.browser_version]) },
          { label: "Platform", render: (row) => textCell(row.platform) },
          { label: "System", render: (row) => detailCell([row.system, row.system_version]) },
          { label: "Endpoint", render: (row) => endpointCell(row.endpoint) },
          { label: "Requests", render: (row) => formatNumber(row.request_count) },
          { label: "Last seen", render: (row) => textCell(formatTime(row.last_seen_at)) },
        ], data.web_usage || []);

        renderTable("recent-table", [
          { label: "Seen at", render: (row) => textCell(formatTime(row.requested_at)) },
          { label: "Method / Status", render: (row) => detailCell([row.method, String(row.status_code ?? "n/a")]) },
          { label: "Client", render: (row) => detailCell([row.client_type, row.platform, row.system, row.system_version]) },
          { label: "Version", render: (row) => detailCell([row.app_version, row.browser_name, row.browser_version]) },
          { label: "Endpoint", render: (row) => endpointCell(row.endpoint) },
          { label: "User-Agent", render: (row) => codeCell(row.user_agent) },
        ], data.recent_requests || []);
      } catch (error) {
        const message = escapeHtml(error?.message || "Unknown error");
        document.getElementById("summary-cards").innerHTML = `<div class="error">${message}</div>`;
        document.getElementById("daily-bars").innerHTML = `<div class="error">${message}</div>`;
        document.getElementById("client-chips").innerHTML = "";
        document.getElementById("platform-chips").innerHTML = "";
        document.getElementById("system-chips").innerHTML = "";
        document.getElementById("endpoint-table").innerHTML = `<div class="error">${message}</div>`;
        document.getElementById("app-table").innerHTML = `<div class="error">${message}</div>`;
        document.getElementById("web-table").innerHTML = `<div class="error">${message}</div>`;
        document.getElementById("recent-table").innerHTML = `<div class="error">${message}</div>`;
        document.getElementById("generated-label").textContent = "Unable to load analytics";
      }
    }

    filtersForm.addEventListener("submit", (event) => {
      event.preventDefault();
      writeFiltersToUrl(daysInput.value || "7", limitInput.value || "20");
      loadDashboard();
    });

    loadDashboard();
  </script>
</body>
</html>
"""
