/* =========================================================================
   OPSMESH sidebar helper — renders the consistent sidebar shell.
   Each page calls OPSMESH.renderShell("page-id") at load.
   ========================================================================= */
(function (global) {
  "use strict";

  const NAV_ITEMS = [
    { id: "overview",     href: "/ui/dashboard.html",     label: "Overview" },
    { id: "leads",        href: "/ui/leads.html",          label: "Leads" },
    { id: "runs",         href: "/ui/runs.html",           label: "Runs" },
    { id: "approvals",    href: "/ui/approvals.html",      label: "Approvals" },
    { id: "integrations", href: "/ui/integrations.html",   label: "Integrations" },
  ];

  function renderShell(activePageId, mainContentHtml) {
    const user = (global.OPSMESH && global.OPSMESH.auth.user) || {};
    const email = user.email || "unknown";
    const mode = user.mode || "dev";

    const nav = NAV_ITEMS.map(function (item) {
      const cls = item.id === activePageId ? "active" : "";
      return '<a href="' + item.href + '" class="' + cls + '">' + item.label + "</a>";
    }).join("");

    return '' +
      '<div class="app-shell">' +
        '<aside class="sidebar">' +
          '<div class="brand"><span class="dot">●</span> OPSMESH</div>' +
          '<nav>' + nav + "</nav>" +
          '<div class="user">' +
            '<div class="email">' + global.OPSMESH.ui.escape(email) + "</div>" +
            '<div>mode: ' + mode + '</div>' +
            '<button id="logout-btn">Sign out</button>' +
          "</div>" +
        "</aside>" +
        '<main class="main">' + (mainContentHtml || "") + "</main>" +
      "</div>";
  }

  // Wire up the logout button after DOM is built
  function attachShellHandlers() {
    const btn = document.getElementById("logout-btn");
    if (btn) {
      btn.addEventListener("click", function () {
        global.OPSMESH.auth.logout();
      });
    }
  }

  global.OPSMESH = global.OPSMESH || {};
  global.OPSMESH.renderShell = renderShell;
  global.OPSMESH.attachShellHandlers = attachShellHandlers;
})(window);
