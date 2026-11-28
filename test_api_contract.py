"""
Contract tests between the browser API client (static/js/api.js) and the backend.

api.js can only be exercised for BEHAVIOUR by tests_frontend/test_api_client.mjs (Node). What those
tests can not see is whether api.js and the backend still AGREE on the facts both sides hard-code:
header names, the version format, the update-required code and status, the CORS allow-list, and the
/api/app-config shape. Any drift here would break the Android app silently (a CORS preflight that the
server rejects, a version string the server ignores), so this file reads api.js as text and compares it
with the real backend modules.

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_api_contract.py -v
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient

from backend import client_access as ca
import backend.main as main_module

REPO = os.path.abspath(os.path.dirname(__file__))
API_JS = os.path.join(REPO, "static", "js", "api.js")
FIXTURE = os.path.join(REPO, "tests_frontend", "fixtures", "app_config.contract.json")


def js():
    return open(API_JS, encoding="utf-8").read()


def js_code():
    """api.js without its header block comment and without full-line // comments."""
    text = re.sub(r"^\s*/\*.*?\*/", "", js(), count=1, flags=re.S)
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))


def js_regex(name):
    """The source of a `var NAME = /.../;` regex literal in api.js, as a Python-compatible pattern."""
    m = re.search(r"var\s+" + name + r"\s*=\s*/(.+?)/;", js())
    assert m, f"{name} not found in api.js"
    return m.group(1).replace("\\/", "/")


class TestHeaderContract:
    def test_client_version_header_name_matches_the_server(self):
        m = re.search(r'HEADER_CLIENT_VERSION\s*=\s*"([^"]+)"', js())
        assert m.group(1).lower().encode() == ca.CLIENT_VERSION_HEADER

    def test_every_header_the_client_sends_is_inside_the_cors_allow_list(self):
        sent = set(re.findall(r'headers\["([A-Za-z-]+)"\]', js_code()))
        sent |= {"Accept"}  # a CORS-safelisted request header; never needs listing
        sent.add(re.search(r'HEADER_CLIENT_VERSION\s*=\s*"([^"]+)"', js()).group(1))
        allowed = {h.lower() for h in ca.CORS_ALLOWED_HEADERS} | {"accept"}
        assert {h.lower() for h in sent} <= allowed, sorted(h for h in sent if h.lower() not in allowed)

    def test_request_id_is_a_response_header_only_exposed_to_javascript(self):
        assert "X-Request-ID" in ca.CORS_EXPOSED_HEADERS, "the browser could not read it otherwise"
        assert "X-Request-ID" not in ca.CORS_ALLOWED_HEADERS, "server allow-list changed: revisit api.js, which never sends it"
        assert not re.search(r'headers\[\s*["\']X-Request-ID', js_code(), re.I)
        assert "Retry-After" in ca.CORS_EXPOSED_HEADERS, "api.js reads Retry-After"

    def test_the_server_really_ignores_an_inbound_request_id_header(self):
        """api.js relies on the server generating its own id; if the server started trusting an inbound one, revisit."""
        source = open(os.path.join(REPO, "backend", "main.py"), encoding="utf-8").read()
        assert not re.search(r'headers\.get\(\s*["\']x-request-id', source, re.I)
        assert not re.search(r'Header\([^)]*x[-_]request[-_]id', source, re.I)

    def test_a_real_preflight_for_the_headers_api_js_sends_is_accepted(self):
        client = TestClient(main_module.app)
        r = client.options(
            "/api/chat",
            headers={
                "Origin": ca.ANDROID_APP_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,x-client-version",
            },
        )
        assert r.status_code == 200
        allowed = r.headers.get("access-control-allow-headers", "").lower()
        for h in ("authorization", "content-type", "x-client-version"):
            assert h in allowed
        bad = client.options(
            "/api/chat",
            headers={"Origin": ca.ANDROID_APP_ORIGIN, "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "x-request-id"},
        )
        assert bad.status_code == 400, "an X-Request-ID request header WOULD be rejected: api.js must never send one"


class TestVersionContract:
    @pytest.mark.parametrize("value", [
        "android/1.2.3", "android/0.0.0", "android/1234.1234.1234", "web/1.0.0",
        "android/1.2", "Android/1.2.3", "android/1.2.3 ", "android/1.2.3.4", "android/12345.1.1",
        "android/1.2.3\r\nX-A: b", "a/1.2.3", "x" * 17 + "/1.2.3", "", "android", "1.2.3",
    ])
    def test_client_version_syntax_is_identical_on_both_sides(self, value):
        client = re.compile(js_regex("CLIENT_VERSION_RE"))
        server_ok = ca.parse_client_version(value) is not None
        # the server .strip()s; api.js is stricter on purpose (it never sends whitespace)
        assert bool(client.match(value)) <= server_ok
        if value == value.strip() and len(value) <= 40:
            assert bool(client.match(value)) == server_ok, value

    @pytest.mark.parametrize("value", ["1.2.3", "0.0.0", "1234.1234.1234", "1.2", "1.2.3.4", "01.2.3", "a.b.c", "12345.1.1", " 1.2.3", ""])
    def test_version_syntax_matches_parse_version(self, value):
        client = re.compile(js_regex("VERSION_RE"))
        assert bool(client.match(value.strip())) == (ca.parse_version(value) is not None)

    def test_update_required_code_and_status_match_the_server(self):
        assert re.search(r'ERROR_UPDATE_REQUIRED\s*=\s*"([^"]+)"', js()).group(1) == ca.ERROR_APP_UPDATE_REQUIRED
        assert ca.APP_UPDATE_REQUIRED_STATUS == 400
        assert re.search(r"response\.status === 400 && info\.code === ERROR_UPDATE_REQUIRED", js_code()), \
            "api.js must act on the status AND the structured code, never the status alone"

    def test_the_real_gate_response_is_what_api_js_expects(self, monkeypatch):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "2.0.0")
        client = TestClient(main_module.app)
        r = client.get("/api/profile", headers={"X-Client-Version": "android/1.0.0"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["code"] == "APP_UPDATE_REQUIRED" and isinstance(detail["message"], str) and detail["min_version"] == "2.0.0"
        assert r.headers["cache-control"] == "no-store"

    def test_the_gate_never_refuses_a_website_request(self, monkeypatch):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "9.9.9")
        r = TestClient(main_module.app).get("/api/app-config")
        assert r.status_code == 200


class TestAppConfigContract:
    def test_fixture_equals_the_real_endpoint_output(self, monkeypatch):
        monkeypatch.delenv("MIN_ANDROID_VERSION", raising=False)
        monkeypatch.delenv("LATEST_ANDROID_VERSION", raising=False)
        r = TestClient(main_module.app).get("/api/app-config")
        assert r.status_code == 200
        assert r.json() == json.load(open(FIXTURE, encoding="utf-8")), (
            "the JSON fixture used by the Node tests is out of date - regenerate it from backend.client_access.build_app_config()"
        )

    def test_the_keys_api_js_reads_exist_in_the_response(self, monkeypatch):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "1.3.0")
        monkeypatch.setenv("LATEST_ANDROID_VERSION", "1.5.0")
        body = TestClient(main_module.app).get("/api/app-config").json()
        for needle in ("data.api_version", "android.min_version", "android.latest_version", "data.features", "account_deletion"):
            assert needle in js_code(), needle
        assert body["api_version"] == 1 and body["android"] == {"min_version": "1.3.0", "latest_version": "1.5.0"}
        assert set(body["features"]) == {"account_deletion"} and isinstance(body["features"]["account_deletion"], bool)
        assert re.search(r"SUPPORTED_API_VERSION\s*=\s*(\d+)", js()).group(1) == str(ca.API_VERSION)

    def test_app_config_is_public_and_not_cached_by_intermediaries(self):
        r = TestClient(main_module.app).get("/api/app-config")
        assert r.headers["cache-control"] == "no-cache"
        assert "authorization" not in {k.lower() for k in r.headers}


class TestFileHygiene:
    def test_api_js_is_the_single_network_call_site_and_holds_no_secret(self):
        code = js_code()
        assert len(re.findall(r"\bfetch\(", code)) == 1
        assert not re.search(r"service[_-]?role|SUPABASE_SERVICE|api[_-]?key|localStorage|sessionStorage|console\.", code, re.I)

    def test_the_existing_backend_secret_guards_still_hold_for_the_new_file(self):
        # mirrors test_usage_telemetry_integrity.TestStaticGuards for static/ files
        assert not re.search(r"SERVICE_ROLE|service_role_key|SUPABASE_SERVICE", js(), re.I)
        assert "USAGE_WRITE_KEY" not in js()