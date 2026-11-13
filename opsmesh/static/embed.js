/*!
 * OPSMESH Embed Widget v2.0
 * Drop-in lead-capture form for static sites (no backend required).
 *
 * Usage (simplest - endpoint is auto-derived from where this script loads):
 *   <script src="https://your-opsmesh-host/ui/embed.js" async></script>
 *   <div id="opsmesh-form"
 *        data-token="pk_live_xxxxxxxxxxxxxxxx"
 *        data-lead-type="company"></div>
 *
 * Optional attributes:
 *   data-endpoint   - override the auto-derived /api/v1/public/lead URL
 *   data-redirect   - URL to redirect to after successful submit
 *   data-heading    - form heading (default "Get in touch")
 *   data-subheading - subtext under the heading
 *   data-color      - accent color hex (default #6366f1)
 *   data-lead-type  - "company" | "individual" (default "company")
 *
 * The token is a publishable key (set OPSMESH_PUBLIC_TOKEN on the server).
 * It can only create leads - never read, send, or approve.
 * CORS is open on the public endpoint, so this works from any origin.
 *
 * v2.0 changes:
 *   - Auto-derives the endpoint from the script's own src URL (no manual
 *     data-endpoint needed - matches the Stripe/Intercom pattern).
 *   - Honeypot field for bot protection (silently swallowed).
 *   - Server-side validation mirrored client-side.
 *   - Accent color customization via data-color.
 *   - Graceful error when the public token is not configured server-side.
 */
(function () {
  "use strict";

  // Capture this script's src URL at load time (document.currentScript is
  // null after DOMContentLoaded, so we grab it synchronously here).
  var SCRIPT_SRC = "";
  try {
    SCRIPT_SRC = (document.currentScript && document.currentScript.src) || "";
  } catch (e) {}

  function deriveEndpoint(src) {
    if (!src) return "";
    try {
      var u = new URL(src, window.location.href);
      return u.origin + "/api/v1/public/lead";
    } catch (e) {
      return "";
    }
  }
  var DEFAULT_ENDPOINT = deriveEndpoint(SCRIPT_SRC);

  function findContainer() {
    // Prefer the explicit id; fall back to the first [data-opsmesh-form].
    var el = document.getElementById("opsmesh-form");
    if (el) return el;
    var all = document.querySelectorAll("[data-opsmesh-form]");
    return all.length ? all[0] : null;
  }

  function buildCss(accent) {
    var a = accent || "#6366f1";
    // a darker shade for hover (naive: just shift down; kept simple + safe)
    return '' +
      '.opsmesh-embed{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;max-width:440px;margin:0 auto;padding:24px;border-radius:12px;background:#fff;color:#1a1a1a;box-shadow:0 1px 3px rgba(0,0,0,0.08)}' +
      '.opsmesh-embed h3{margin:0 0 4px;font-size:18px;font-weight:700}' +
      '.opsmesh-embed p.sub{margin:0 0 18px;font-size:13px;color:#6b7280}' +
      '.opsmesh-embed label{display:block;font-size:12px;font-weight:600;color:#374151;margin:0 0 4px}' +
      '.opsmesh-embed .field{margin-bottom:14px}' +
      '.opsmesh-embed input,.opsmesh-embed select,.opsmesh-embed textarea{width:100%;padding:9px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:14px;font-family:inherit;box-sizing:border-box;background:#fff;color:#1a1a1a}' +
      '.opsmesh-embed input:focus,.opsmesh-embed textarea:focus{outline:none;border-color:' + a + ';box-shadow:0 0 0 3px ' + a + '26}' +
      '.opsmesh-embed button.submit{width:100%;padding:11px;background:' + a + ';color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}' +
      '.opsmesh-embed button.submit:hover{filter:brightness(0.92)}' +
      '.opsmesh-embed button.submit:disabled{opacity:0.6;cursor:not-allowed}' +
      '.opsmesh-embed .msg{margin-top:12px;padding:10px 12px;border-radius:8px;font-size:13px;display:none}' +
      '.opsmesh-embed .msg.ok{display:block;background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}' +
      '.opsmesh-embed .msg.err{display:block;background:#fef2f2;color:#991b1b;border:1px solid #fecaca}' +
      '.opsmesh-embed .brand{margin-top:14px;text-align:center;font-size:11px;color:#9ca3af}' +
      '.opsmesh-embed .brand a{color:#6b7280;text-decoration:none}' +
      /* honeypot: invisible to humans, visible to bots that fill every field */
      '.opsmesh-embed .hp{position:absolute;left:-9999px;top:auto;width:1px;height:1px;overflow:hidden}';
  }

  function injectStyles(accent) {
    var id = "opsmesh-embed-css";
    var existing = document.getElementById(id);
    if (existing) existing.remove();
    var s = document.createElement("style");
    s.id = id;
    s.textContent = buildCss(accent);
    document.head.appendChild(s);
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function validEmail(s) {
    return /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/.test(s);
  }

  function init() {
    var container = findContainer();
    if (!container || container.dataset.opsmeshInit === "1") return;
    container.dataset.opsmeshInit = "1";

    var token = container.getAttribute("data-token") || "";
    var endpoint = container.getAttribute("data-endpoint") || DEFAULT_ENDPOINT;
    var redirect = container.getAttribute("data-redirect") || "";
    var leadType = container.getAttribute("data-lead-type") || "company";
    var heading = container.getAttribute("data-heading") || "Get in touch";
    var sub = container.getAttribute("data-subheading") || "Tell us about your project. We'll get back to you within a day.";
    var color = container.getAttribute("data-color") || "#6366f1";

    if (leadType !== "company" && leadType !== "individual") leadType = "company";
    injectStyles(color);

    // Hard-fail with a visible message if we can't determine the endpoint.
    if (!endpoint) {
      container.innerHTML =
        '<div class="opsmesh-embed"><div class="msg err" style="display:block">' +
        "OPSMESH embed: could not determine the endpoint. " +
        "Either load this script from your OPSMESH host, or set " +
        "<code>data-endpoint</code> on the container.</div></div>";
      return;
    }

    container.innerHTML =
      '<div class="opsmesh-embed">' +
        "<h3>" + esc(heading) + "</h3>" +
        '<p class="sub">' + esc(sub) + "</p>" +
        '<form id="opsmesh-embed-form" novalidate>' +
          (leadType === "company"
            ? '<div class="field"><label>Company</label><input name="company" type="text" placeholder="Acme Corp" required maxlength="200"></div>'
            : "") +
          '<div class="field"><label>Your name</label><input name="contact_name" type="text" placeholder="Jane Doe" required maxlength="200"></div>' +
          '<div class="field"><label>Email</label><input name="contact_email" type="email" placeholder="jane@acme.com" required maxlength="320"></div>' +
          (leadType === "company"
            ? '<div class="field"><label>Industry <span style="font-weight:400;color:#9ca3af">(optional)</span></label><input name="industry" type="text" placeholder="Software" maxlength="120"></div>'
            : '<div class="field"><label>What can we help with? <span style="font-weight:400;color:#9ca3af">(optional)</span></label><textarea name="notes" rows="3" placeholder="A sentence about your project" maxlength="2000"></textarea></div>') +
          /* honeypot - bots fill this; humans never see it */
          '<div class="hp" aria-hidden="true"><label>Website</label><input name="website_hp" type="text" tabindex="-1" autocomplete="off"></div>' +
          '<button class="submit" type="submit">Submit</button>' +
          '<div class="msg" id="opsmesh-embed-msg"></div>' +
        "</form>" +
        '<div class="brand">Powered by <a href="https://opsmesh.dev" target="_blank" rel="noopener">OPSMESH</a></div>' +
      "</div>";

    var form = container.querySelector("#opsmesh-embed-form");
    var msg = container.querySelector("#opsmesh-embed-msg");

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      msg.className = "msg";
      msg.textContent = "";

      var data = {
        lead_type: leadType,
        submission_id: "emb_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8)
      };
      var inputs = form.querySelectorAll("input, textarea, select");
      inputs.forEach(function (inp) {
        if (inp.name) data[inp.name] = inp.value.trim();
      });

      // Honeypot: if the hidden field is filled, the submitter is a bot.
      // Show the success message and abort - the bot is fooled, no lead is
      // created. The server also checks (direct API callers can't bypass).
      if (data.website_hp) {
        msg.className = "msg ok";
        msg.textContent = "Thanks! We'll be in touch soon.";
        form.querySelectorAll("input, textarea").forEach(function (inp) { if (inp.type !== "hidden") inp.value = ""; });
        return;
      }
      delete data.website_hp;

      if (!validEmail(data.contact_email)) {
        msg.className = "msg err";
        msg.textContent = "Please enter a valid email address.";
        return;
      }
      if (!data.contact_name) {
        msg.className = "msg err";
        msg.textContent = "Please enter your name.";
        return;
      }
      if (leadType === "company" && !data.company) {
        msg.className = "msg err";
        msg.textContent = "Please enter a company name.";
        return;
      }

      var btn = form.querySelector("button.submit");
      btn.disabled = true;
      btn.textContent = "Sending...";

      fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer " + token
        },
        body: JSON.stringify(data)
      }).then(function (r) {
        return r.json().then(function (body) {
          return { status: r.status, body: body };
        });
      }).then(function (res) {
        btn.disabled = false;
        btn.textContent = "Submit";
        if (res.status >= 200 && res.status < 300) {
          msg.className = "msg ok";
          msg.textContent = "Thanks! We'll be in touch soon.";
          form.querySelectorAll("input, textarea").forEach(function (inp) { if (inp.type !== "hidden") inp.value = ""; });
          if (redirect) {
            setTimeout(function () { window.location.href = redirect; }, 900);
          }
        } else if (res.status === 401) {
          msg.className = "msg err";
          msg.textContent = "This form is not activated yet. The site owner needs to set OPSMESH_PUBLIC_TOKEN on the OPSMESH server.";
        } else {
          msg.className = "msg err";
          msg.textContent = (res.body && res.body.detail) || (res.body && res.body.error && res.body.error.message) || "Submission failed. Please try again.";
        }
      }).catch(function () {
        btn.disabled = false;
        btn.textContent = "Submit";
        msg.className = "msg err";
        msg.textContent = "Network error. Please check your connection and try again.";
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
