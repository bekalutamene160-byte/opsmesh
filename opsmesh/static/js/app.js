/* =========================================================================
   OPSMESH API client + auth helpers (vanilla JS, no dependencies)

   Exposes a single global `OPSMESH` object with:
     - api.get(path, params)    -> Promise<any>
     - api.post(path, body)     -> Promise<any>
     - auth.token, auth.user, auth.login(token), auth.logout()
     - ui.toast(msg, kind), ui.escape(str), ui.formatDate(iso)
   ========================================================================= */
(function (global) {
  "use strict";

  // ---- Token storage (localStorage) ----
  const TOKEN_KEY = "opsmesh_token";
  const USER_KEY = "opsmesh_user";

  function readToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }
  function writeToken(t) {
    try {
      if (t) { localStorage.setItem(TOKEN_KEY, t); }
      else { localStorage.removeItem(TOKEN_KEY); }
    } catch (e) { /* ignore quota errors */ }
  }
  function readUser() {
    try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); }
    catch (e) { return null; }
  }
  function writeUser(u) {
    try {
      if (u) { localStorage.setItem(USER_KEY, JSON.stringify(u)); }
      else { localStorage.removeItem(USER_KEY); }
    } catch (e) { /* ignore */ }
  }

  // ---- Auth module ----
  const auth = {
    token: readToken(),
    user: readUser(),

    async login(token) {
      // Set the token, then call /api/v1/health to verify and fetch user info
      writeToken(token || "");
      this.token = token || "";
      try {
        const health = await api.get("/api/v1/health");
        const user = {
          // In dev mode we synthesize a dev user; in prod mode we'd parse the JWT
          email: token ? "(JWT user)" : "dev@opsmesh.dev",
          workspace_id: "default",
          mode: health.auth ? health.auth.mode : "dev",
        };
        writeUser(user);
        this.user = user;
        return user;
      } catch (e) {
        // Health endpoint failed — token may be invalid
        writeToken("");
        writeUser(null);
        this.token = "";
        this.user = null;
        throw e;
      }
    },

    async loginWithGoogle(googleCredential) {
      // POST the Google ID token to /api/v1/auth/google.
      // Backend verifies it (via Google's JWKS) and returns a session JWT.
      // We store the SESSION JWT (not Google's token).
      try {
        const result = await fetch("/api/v1/auth/google", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ credential: googleCredential }),
        }).then(function (r) {
          if (!r.ok) {
            return r.json().then(function (data) {
              throw new Error(
                (data && data.detail) ||
                (data && data.error && data.error.message) ||
                "Google sign-in failed (" + r.status + ")"
              );
            });
          }
          return r.json();
        });

        if (!result || !result.token) {
          throw new Error("Backend didn't return a session token");
        }

        // Store the SESSION JWT (HS256, signed by the backend)
        writeToken(result.token);
        this.token = result.token;

        const user = {
          email: result.user.email,
          workspace_id: result.user.workspace_id,
          mode: "google_oauth",
        };
        writeUser(user);
        this.user = user;
        return user;
      } catch (e) {
        writeToken("");
        writeUser(null);
        this.token = "";
        this.user = null;
        throw e;
      }
    },

    logout() {
      writeToken("");
      writeUser(null);
      this.token = "";
      this.user = null;
      window.location.href = "/ui/login.html";
    },

    requireAuth() {
      // For pages that require auth — redirect to login if no token AND
      // the server is in production mode. We can't know server mode without
      // calling /api/v1/health, so we attempt the call and react.
      // Returns a Promise that resolves to true (authed) or redirects.
      return api.get("/api/v1/health").then(function (health) {
        const mode = (health.auth && health.auth.mode) || "dev";
        if (mode === "dev" && !auth.token) {
          // Dev mode: no token required. Synthesize a dev user.
          auth.user = { email: "dev@opsmesh.dev", workspace_id: "default", mode: "dev" };
          writeUser(auth.user);
          return true;
        }
        if (mode !== "dev" && !auth.token) {
          // Production mode and no token → redirect to login
          window.location.href = "/ui/login.html";
          return false;
        }
        return true;
      }).catch(function () {
        // Network or server error — redirect to login
        window.location.href = "/ui/login.html";
        return false;
      });
    },
  };

  // ---- API client ----
  async function request(method, path, params, body) {
    let url = path;
    if (params && Object.keys(params).length) {
      const qs = new URLSearchParams();
      for (const k in params) {
        if (params[k] !== undefined && params[k] !== null) qs.append(k, params[k]);
      }
      url = path + "?" + qs.toString();
    }
    const headers = { "Accept": "application/json" };
    if (auth.token) headers["Authorization"] = "Bearer " + auth.token;
    if (body !== undefined) headers["Content-Type"] = "application/json";

    const opts = { method, headers };
    if (body !== undefined) opts.body = JSON.stringify(body);

    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (e) {
      const err = new Error("Network error: " + e.message);
      err.kind = "network";
      throw err;
    }

    let data = null;
    const ct = resp.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      try { data = await resp.json(); } catch (e) { data = null; }
    } else {
      try { data = await resp.text(); } catch (e) { data = null; }
    }

    if (!resp.ok) {
      const detail = (data && data.detail) ||
                     (data && data.error && data.error.message) ||
                     resp.statusText ||
                     "Request failed";
      const err = new Error(detail);
      err.kind = "http";
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  const api = {
    get: (path, params) => request("GET", path, params, undefined),
    post: (path, body) => request("POST", path, undefined, body),
    put: (path, body) => request("PUT", path, undefined, body),
    del: (path) => request("DELETE", path, undefined, undefined),
  };

  // ---- UI helpers ----
  const ui = {
    toast(message, kind) {
      kind = kind || "info";
      const el = document.createElement("div");
      el.className = "toast " + kind;
      el.textContent = message;
      document.body.appendChild(el);
      setTimeout(function () {
        el.style.opacity = "0";
        el.style.transition = "opacity 0.3s";
        setTimeout(function () { el.remove(); }, 300);
      }, 4000);
    },

    escape(s) {
      if (s === null || s === undefined) return "";
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    },

    formatDate(iso) {
      if (!iso) return "—";
      try {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return iso;
        const pad = (n) => String(n).padStart(2, "0");
        return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" +
               pad(d.getUTCDate()) + " " + pad(d.getUTCHours()) + ":" +
               pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds()) + " UTC";
      } catch (e) {
        return iso;
      }
    },

    relativeTime(iso) {
      if (!iso) return "—";
      try {
        const d = new Date(iso);
        const now = Date.now();
        const diff = (now - d.getTime()) / 1000;
        if (diff < 60) return Math.floor(diff) + "s ago";
        if (diff < 3600) return Math.floor(diff / 60) + "m ago";
        if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
        if (diff < 86400 * 30) return Math.floor(diff / 86400) + "d ago";
        return ui.formatDate(iso);
      } catch (e) {
        return iso;
      }
    },

    stateBadge(state) {
      const map = {
        new: "neutral",
        researching: "info",
        awaiting_evidence_review: "warning",
        evidence_provided: "info",
        qualifying: "info",
        drafting: "info",
        critic: "info",
        awaiting_approval: "warning",
        sending: "info",
        send_unknown: "warning",
        crm_updating: "info",
        sent: "success",
        rejected: "danger",
        failed: "danger",
        stopped_by_user: "neutral",
        approval_expired: "neutral",
      };
      const kind = map[state] || "neutral";
      return '<span class="badge ' + kind + '">' + ui.escape(state) + "</span>";
    },

    showLoading(target, message) {
      const el = typeof target === "string" ? document.querySelector(target) : target;
      if (!el) return;
      el.innerHTML = '<div class="empty"><span class="spinner"></span> <span>' +
        ui.escape(message || "Loading...") + "</span></div>";
    },

    showEmpty(target, title, hint) {
      const el = typeof target === "string" ? document.querySelector(target) : target;
      if (!el) return;
      el.innerHTML = '<div class="empty"><div class="icon">·</div>' +
        '<div class="title">' + ui.escape(title || "Nothing here") + "</div>" +
        (hint ? '<div>' + ui.escape(hint) + "</div>" : "") + "</div>";
    },

    showError(target, message) {
      const el = typeof target === "string" ? document.querySelector(target) : target;
      if (!el) return;
      el.innerHTML = '<div class="card"><h2>Error</h2><p style="color: var(--danger);">' +
        ui.escape(message || "An error occurred") + "</p></div>";
    },
  };

  // ---- Expose globally ----
  global.OPSMESH = { api, auth, ui };

  // ---- Global error handler ----
  window.addEventListener("unhandledrejection", function (event) {
    console.error("Unhandled promise rejection:", event.reason);
    ui.toast(event.reason && event.reason.message ? event.reason.message : "Unexpected error", "danger");
  });
})(window);
