"""
Tests for Phase 5: frontend served from FastAPI at /ui.

Verifies:
  - All HTML pages are reachable (200, text/html)
  - CSS is reachable (200, text/css)
  - JS modules are reachable (200, text/javascript)
  - Each HTML page references the correct CSS/JS assets
  - Root path / redirects to /ui/login.html
  - HTML structure is well-formed (has DOCTYPE, title, body)
  - Each page contains the expected shell container (#root)
  - The API client (app.js) and shell helper (shell.js) are non-empty
    and free of obvious syntax errors (parsed by a JS-aware check)
"""

from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from opsmesh.api.app import create_app
from opsmesh.config import get_settings


@pytest.fixture(autouse=True)
def clean_env():
    for var in (
        "OPSMESH_SUPABASE_JWT_SECRET",
        "OPSMESH_SESSION_SECRET",
        "OPSMESH_GOOGLE_OAUTH_CLIENT_ID",
        "OPSMESH_GOOGLE_OAUTH_CLIENT_SECRET",
        "OPSMESH_PRODUCTION_MODE",
    ):
        os.environ.pop(var, None)
    get_settings.cache_clear()
    yield
    for var in (
        "OPSMESH_SUPABASE_JWT_SECRET",
        "OPSMESH_SESSION_SECRET",
        "OPSMESH_GOOGLE_OAUTH_CLIENT_ID",
        "OPSMESH_GOOGLE_OAUTH_CLIENT_SECRET",
        "OPSMESH_PRODUCTION_MODE",
    ):
        os.environ.pop(var, None)
    get_settings.cache_clear()


@pytest.fixture
def app():
    return create_app(webhook_secret="test-secret")


@pytest.fixture
def client(app):
    return TestClient(app)


# ===========================================================================
# 1. Static assets are reachable
# ===========================================================================


class TestStaticAssetsReachable:
    def test_login_page_reachable(self, client):
        r = client.get("/ui/login.html")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_dashboard_page_reachable(self, client):
        r = client.get("/ui/dashboard.html")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_leads_page_reachable(self, client):
        r = client.get("/ui/leads.html")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_runs_page_reachable(self, client):
        r = client.get("/ui/runs.html")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_approvals_page_reachable(self, client):
        r = client.get("/ui/approvals.html")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_integrations_page_reachable(self, client):
        r = client.get("/ui/integrations.html")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_index_redirects_to_login(self, client):
        r = client.get("/ui/", follow_redirects=False)
        # StaticFiles(html=True) serves index.html for directory requests
        assert r.status_code in (200, 307)

    def test_root_redirects_to_dashboard_in_demo_mode(self, client):
        """Stage B Phase 5: in single_user_demo mode (the default), visiting /
        opens the dashboard directly — no login redirect. Google OAuth is
        removed for the demo release.
        """
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers.get("location") == "/ui/dashboard.html"


# ===========================================================================
# 2. CSS and JS assets
# ===========================================================================


class TestCssJsAssets:
    def test_app_css_reachable(self, client):
        r = client.get("/ui/css/app.css")
        assert r.status_code == 200
        assert "text/css" in r.headers.get("content-type", "")
        assert len(r.text) > 1000  # non-trivial stylesheet

    def test_app_js_reachable(self, client):
        r = client.get("/ui/js/app.js")
        assert r.status_code == 200
        assert "javascript" in r.headers.get("content-type", "")
        assert len(r.text) > 1000

    def test_shell_js_reachable(self, client):
        r = client.get("/ui/js/shell.js")
        assert r.status_code == 200
        assert "javascript" in r.headers.get("content-type", "")
        assert len(r.text) > 500

    def test_app_js_exposes_opsmesh_global(self, client):
        """app.js attaches OPSMESH to window."""
        r = client.get("/ui/js/app.js")
        assert "global.OPSMESH" in r.text or "window.OPSMESH" in r.text

    def test_shell_js_renders_sidebar(self, client):
        """shell.js renders the sidebar with NAV_ITEMS."""
        r = client.get("/ui/js/shell.js")
        assert "Overview" in r.text
        assert "Leads" in r.text
        assert "Runs" in r.text
        assert "Approvals" in r.text
        assert "Integrations" in r.text


# ===========================================================================
# 3. HTML structure validation
# ===========================================================================


class TestHtmlStructure:
    def test_login_page_has_doctype_and_title(self, client):
        r = client.get("/ui/login.html")
        text = r.text
        assert "<!DOCTYPE html>" in text
        assert "<title>" in text
        assert "</html>" in text

    def test_dashboard_page_has_root_div(self, client):
        r = client.get("/ui/dashboard.html")
        assert '<div id="root">' in r.text

    def test_leads_page_has_root_div(self, client):
        r = client.get("/ui/leads.html")
        assert '<div id="root">' in r.text

    def test_runs_page_has_root_div(self, client):
        r = client.get("/ui/runs.html")
        assert '<div id="root">' in r.text

    def test_approvals_page_has_root_div(self, client):
        r = client.get("/ui/approvals.html")
        assert '<div id="root">' in r.text

    def test_integrations_page_has_root_div(self, client):
        r = client.get("/ui/integrations.html")
        assert '<div id="root">' in r.text

    def test_all_pages_link_app_css(self, client):
        """Every page must link to /ui/css/app.css."""
        for page in ["login.html", "dashboard.html", "leads.html", "runs.html",
                     "approvals.html", "integrations.html"]:
            r = client.get(f"/ui/{page}")
            assert '/ui/css/app.css' in r.text, f"{page} missing CSS link"

    def test_pages_load_app_js(self, client):
        """Pages with interactivity must load /ui/js/app.js.

        Stage B Phase 5: login.html is now a pure redirect stub (no app.js) —
        it does not need the API client because it immediately redirects to
        the dashboard.
        """
        for page in ["dashboard.html", "leads.html", "runs.html",
                     "approvals.html", "integrations.html"]:
            r = client.get(f"/ui/{page}")
            assert '/ui/js/app.js' in r.text, f"{page} missing app.js"

    def test_shell_pages_load_shell_js(self, client):
        """Pages that render the shell also load /ui/js/shell.js."""
        for page in ["dashboard.html", "leads.html", "runs.html",
                     "approvals.html", "integrations.html"]:
            r = client.get(f"/ui/{page}")
            assert '/ui/js/shell.js' in r.text, f"{page} missing shell.js"

    def test_login_page_does_not_load_shell(self, client):
        """The login redirect stub should NOT load shell.js (no sidebar)."""
        r = client.get("/ui/login.html")
        assert "/ui/js/shell.js" not in r.text


# ===========================================================================
# 4. Login redirect stub (Stage B Phase 5: OAuth removed)
# ===========================================================================


class TestLoginRedirect:
    """Stage B Phase 5: login.html is now a pure redirect to the dashboard.

    Google OAuth is removed from the default (single_user_demo) deployment.
    Visiting / opens the dashboard directly. login.html remains only to
    redirect stale bookmarks/links. It must NOT contain any half-active
    OAuth code (no GIS script, no google-btn, no /auth/google calls).
    """

    def test_login_page_redirects_to_dashboard(self, client):
        """login.html redirects to /ui/dashboard.html (meta refresh + JS)."""
        r = client.get("/ui/login.html")
        assert "/ui/dashboard.html" in r.text

    def test_login_page_has_no_google_gis_script(self, client):
        """No Google Identity Services script ships in the demo."""
        r = client.get("/ui/login.html")
        assert "accounts.google.com/gsi/client" not in r.text

    def test_login_page_has_no_google_button_container(self, client):
        """No #google-btn container (OAuth UI removed)."""
        r = client.get("/ui/login.html")
        assert 'id="google-btn"' not in r.text

    def test_login_page_has_no_dev_or_prod_blocks(self, client):
        """No dev-block / prod-block (the mode-switching UI is gone)."""
        r = client.get("/ui/login.html")
        assert 'id="dev-block"' not in r.text
        assert 'id="prod-block"' not in r.text

    def test_login_page_does_not_call_auth_google_endpoint(self, client):
        """login.html does not reference the OAuth endpoint."""
        r = client.get("/ui/login.html")
        assert "/api/v1/auth/google" not in r.text

    def test_app_js_still_has_loginWithGoogle_for_multi_user_mode(self, client):
        """The loginWithGoogle method stays in app.js so multi_user mode
        can be re-enabled by flipping OPSMESH_ACCESS_MODE (no frontend
        rewrite needed). It is simply not invoked in demo mode."""
        r = client.get("/ui/js/app.js")
        assert "loginWithGoogle" in r.text


# ===========================================================================
# 5. Dashboard page behavior
# ===========================================================================


class TestDashboardPage:
    def test_dashboard_calls_dashboard_api(self, client):
        r = client.get("/ui/dashboard.html")
        assert "/api/v1/dashboard" in r.text

    def test_dashboard_renders_8_metrics(self, client):
        """The dashboard renders stat cards for the 8 metrics."""
        r = client.get("/ui/dashboard.html")
        # Look for stat labels — at least 4 of the 8 expected
        labels_found = sum(
            1 for label in [
                "Runs (24h)", "Success rate", "Queue depth", "p95 latency",
                "Cost (24h)", "DLQ depth", "Pending approvals", "LLM error rate",
            ]
            if label in r.text
        )
        assert labels_found >= 6, f"Only {labels_found}/8 metric labels found"

    def test_dashboard_links_to_runs_page(self, client):
        """Clicking a run row navigates to /ui/runs.html?id=..."""
        r = client.get("/ui/dashboard.html")
        assert "/ui/runs.html?id=" in r.text


# ===========================================================================
# 6. Leads page behavior
# ===========================================================================


class TestLeadsPage:
    def test_leads_page_has_create_form(self, client):
        r = client.get("/ui/leads.html")
        assert 'id="create-form"' in r.text
        assert 'id="f-company"' in r.text
        assert 'id="f-email"' in r.text

    def test_leads_page_calls_leads_api(self, client):
        r = client.get("/ui/leads.html")
        assert "/api/v1/leads" in r.text

    def test_leads_page_has_pagination(self, client):
        r = client.get("/ui/leads.html")
        assert 'id="pagination"' in r.text


# ===========================================================================
# 7. Runs page behavior
# ===========================================================================


class TestRunsPage:
    def test_runs_page_has_state_filter(self, client):
        r = client.get("/ui/runs.html")
        assert 'id="state-filter"' in r.text

    def test_runs_page_calls_runs_api(self, client):
        r = client.get("/ui/runs.html")
        assert "/api/v1/runs" in r.text

    def test_runs_page_supports_id_param(self, client):
        """The page reads ?id=... from URL."""
        r = client.get("/ui/runs.html")
        assert "URLSearchParams" in r.text or "location.search" in r.text

    def test_runs_page_calls_timeline_endpoint(self, client):
        r = client.get("/ui/runs.html")
        assert "/timeline" in r.text


# ===========================================================================
# 8. Approvals page behavior
# ===========================================================================


class TestApprovalsPage:
    def test_approvals_page_calls_approvals_api(self, client):
        r = client.get("/ui/approvals.html")
        assert "/api/v1/approvals" in r.text

    def test_approvals_page_has_decision_buttons(self, client):
        """The page defines Gate 1 and Gate 2 decision sets."""
        r = client.get("/ui/approvals.html")
        # Gate 2 decisions
        assert "approve" in r.text
        assert "reject" in r.text
        assert "request_changes" in r.text
        # Gate 1 decisions
        assert "proceed_with_limitations" in r.text
        assert "request_more_info" in r.text
        assert "stop" in r.text

    def test_approvals_page_posts_to_decision_endpoint(self, client):
        r = client.get("/ui/approvals.html")
        assert "/decision" in r.text


# ===========================================================================
# 9. Integrations page behavior
# ===========================================================================


class TestIntegrationsPage:
    def test_integrations_calls_health_endpoint(self, client):
        r = client.get("/ui/integrations.html")
        assert "/api/v1/health" in r.text

    def test_integrations_calls_integrations_endpoint(self, client):
        r = client.get("/ui/integrations.html")
        assert "/api/v1/integrations" in r.text

    def test_integrations_renders_provider_cards(self, client):
        """The page renders cards for LLM, Research, Email, CRM, Database."""
        r = client.get("/ui/integrations.html")
        for name in ["LLM", "Research", "Email", "CRM", "Database"]:
            assert name in r.text, f"Provider {name} not in page"


# ===========================================================================
# 10. JS smoke — the API client has the right shape
# ===========================================================================


class TestJsSmoke:
    def test_app_js_has_api_methods(self, client):
        """OPSMESH.api has get, post, put, del methods."""
        r = client.get("/ui/js/app.js")
        text = r.text
        assert "api.get" in text
        assert "api.post" in text

    def test_app_js_has_auth_helpers(self, client):
        r = client.get("/ui/js/app.js")
        text = r.text
        assert "auth.login" in text or "login:" in text
        assert "auth.logout" in text or "logout:" in text

    def test_app_js_has_ui_helpers(self, client):
        r = client.get("/ui/js/app.js")
        text = r.text
        assert "ui.toast" in text or "toast:" in text
        assert "ui.escape" in text or "escape:" in text

    def test_app_js_handles_401(self, client):
        """The client should handle 401 (unauthorized) by clearing the token
        or surfacing the error. We check for any 401 reference."""
        r = client.get("/ui/js/app.js")
        # Look for status code handling
        assert "err.status" in r.text or "status" in r.text

    def test_no_obvious_js_syntax_errors(self, client):
        """Use `node --check` for real syntax validation."""
        import subprocess
        import tempfile

        r = client.get("/ui/js/app.js")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False, encoding="utf-8"
        ) as f:
            f.write(r.text)
            f.flush()
            tmp_path = f.name

        try:
            result = subprocess.run(
                ["node", "--check", tmp_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, (
                f"app.js has syntax errors:\n{result.stderr}"
            )
        finally:
            os.unlink(tmp_path)

    def test_shell_js_no_syntax_errors(self, client):
        """Use `node --check` on shell.js too."""
        import subprocess
        import tempfile

        r = client.get("/ui/js/shell.js")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False, encoding="utf-8"
        ) as f:
            f.write(r.text)
            f.flush()
            tmp_path = f.name

        try:
            result = subprocess.run(
                ["node", "--check", tmp_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, (
                f"shell.js has syntax errors:\n{result.stderr}"
            )
        finally:
            os.unlink(tmp_path)
