/* =========================================================================
   OPSMESH sidebar helper -- renders the consistent sidebar shell.
   Each page calls OPSMESH.renderShell("page-id") at load.
   ========================================================================= */
(function (global) {
  "use strict";

  // Stage B Phase 7: inline SVG icons for each nav section (no icon font dependency).
  // Each icon is a 16x16 stroke path drawn inside the nav <a>.
  const ICONS = {
    overview: '<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg>',
    leads: '<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    runs: '<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
    approvals: '<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>',
    activity: '<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>',
    integrations: '<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7V4h16v3"/><path d="M9 20h6"/><path d="M12 4v16"/></svg>',
  };

  const NAV_ITEMS = [
    { id: "overview",     href: "/ui/dashboard.html",     label: "Overview" },
    { id: "leads",        href: "/ui/leads.html",          label: "Leads" },
    { id: "runs",         href: "/ui/runs.html",           label: "Runs" },
    { id: "approvals",    href: "/ui/approvals.html",      label: "Approvals" },
    { id: "activity",     href: "/ui/activity.html",       label: "Activity" },
    { id: "integrations", href: "/ui/integrations.html",   label: "Integrations" },
  ];

  function renderShell(activePageId, mainContentHtml) {
    const user = (global.OPSMESH && global.OPSMESH.auth && global.OPSMESH.auth.user) || {};
    const email = user.email || "demo@opsmesh.dev";
    const mode = user.mode || "demo";

    const nav = NAV_ITEMS.map(function (item) {
      const cls = item.id === activePageId ? "active" : "";
      const icon = ICONS[item.id] || "";
      return '<a href="' + item.href + '" class="' + cls + '" data-nav="' + item.id + '">'
        + icon + '<span>' + item.label + "</span></a>";
    }).join("");

    // Stage B Phase 5: in demo mode there is no sign-out (no session). Show a
    // "Demo" badge instead. In multi_user mode, show the email + sign-out.
    const isDemo = (mode === "demo" || mode === "dev");
    const userBlock = isDemo
      ? '<div class="mode-badge">' + uiEscape(mode) + " mode</div>" +
        '<div class="email">' + uiEscape(email) + "</div>"
      : '<div class="email">' + uiEscape(email) + "</div>" +
        '<div>mode: ' + uiEscape(mode) + '</div>' +
        '<button id="logout-btn" class="btn ghost">Sign out</button>';

    return '' +
      '<div class="app-shell">' +
        '<aside class="sidebar">' +
          '<div class="brand"><span class="brand-mark">◆</span><span class="brand-text">OPSMESH</span></div>' +
          '<nav>' + nav + "</nav>" +
          '<div class="user">' + userBlock + "</div>" +
        "</aside>" +
        '<main class="main">' + (mainContentHtml || "") + "</main>" +
      "</div>";
  }

  function uiEscape(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // Wire up the logout button after DOM is built (only present in multi_user mode)
  function attachShellHandlers() {
    const btn = document.getElementById("logout-btn");
    if (btn) {
      btn.addEventListener("click", function () {
        if (global.OPSMESH && global.OPSMESH.auth) {
          global.OPSMESH.auth.logout();
        }
      });
    }
  }

  global.OPSMESH = global.OPSMESH || {};
  global.OPSMESH.renderShell = renderShell;
  global.OPSMESH.attachShellHandlers = attachShellHandlers;
})(window);
