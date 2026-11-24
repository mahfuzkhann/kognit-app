"""
Tests for backend/client_access.py (Report 2 X-06 CORS and X-28 client version / app-config).

What must hold:
  * The Android app origin (https://localhost) can call the API; any other origin cannot, and the
    wildcard is never produced - not by default, not by configuration.
  * Same-origin web traffic (no Origin header) is completely unchanged.
  * The version gate is INERT unless MIN_ANDROID_VERSION is set, fails OPEN on a missing or unreadable header,
    compares versions as numbers, refuses only /api/ calls from an outdated android build, and refuses them
    BEFORE any authentication work. The refusal is HTTP 400 with detail.code APP_UPDATE_REQUIRED (NOT 426: RFC 9110
    requires an Upgrade header on 426 and defines it for protocol upgrades). Because an ordinary validation error is
    also a 400, these tests recognise a refusal by its code, never by the status number alone.
  * Error responses (including the update-required refusal) still carry the CORS headers, so the app can read them.
  * /api/app-config is public, secret-free and reachable by an outdated build.

No network: nothing here calls Supabase or Gemini.
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.main as main
from backend import client_access

ANDROID_ORIGIN = "https://localhost"
EVIL_ORIGIN = "https://evil.example"
REFUSAL_STATUS = 400


def _is_refused(r) -> bool:
    """True only for the version-gate refusal: status 400 AND detail.code == APP_UPDATE_REQUIRED."""
    try:
        return r.status_code == REFUSAL_STATUS and r.json()["detail"]["code"] == "APP_UPDATE_REQUIRED"
    except Exception:
        return False


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("MIN_ANDROID_VERSION", raising=False)
    monkeypatch.delenv("LATEST_ANDROID_VERSION", raising=False)
    monkeypatch.delenv("CORS_EXTRA_ORIGINS", raising=False)
    client_access._warned_bad_values.clear()
    return TestClient(main.app)


def _preflight(client, origin, path="/api/chat/stream", method="POST",
               request_headers="authorization,content-type,x-client-version"):
    return client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": request_headers,
        },
    )


# ---------------------------------------------------------------- origin list parsing
class TestAllowedOrigins:
    def test_default_is_exactly_the_android_origin(self, monkeypatch):
        monkeypatch.delenv("CORS_EXTRA_ORIGINS", raising=False)
        assert client_access.allowed_origins(raw_extra="") == [ANDROID_ORIGIN]
        assert client_access.allowed_origins() == [ANDROID_ORIGIN]

    def test_extra_origins_are_added_after_the_builtin_one(self):
        result = client_access.allowed_origins(raw_extra="https://app.example.com, http://localhost:8000")
        assert result == [ANDROID_ORIGIN, "https://app.example.com", "http://localhost:8000"]

    def test_extras_are_lowercased_and_deduplicated(self):
        result = client_access.parse_extra_origins("HTTPS://App.Example.com,https://app.example.com")
        assert result == ["https://app.example.com"]

    def test_the_builtin_origin_cannot_be_removed_or_duplicated(self):
        assert client_access.allowed_origins(raw_extra="https://localhost,https://a.example") == [ANDROID_ORIGIN, "https://a.example"]
        assert client_access.allowed_origins(raw_extra="https://a.example")[0] == ANDROID_ORIGIN

    @pytest.mark.parametrize("bad", [
        "*", "https://*.example.com", "example.com", "ftp://example.com", "https://example.com/", "https://example.com/path",
        "https://example.com?x=1", "https://user@example.com", "https:// example.com", "null", "https://", "http://:80",
        "https://exa mple.com", "https://-bad.example", "https://example.com:abc", "https://example.com:123456",
    ])
    def test_invalid_entries_are_ignored_never_widened(self, bad):
        assert client_access.parse_extra_origins(bad) == []
        assert client_access.allowed_origins(raw_extra=bad) == [ANDROID_ORIGIN]

    def test_one_bad_entry_does_not_discard_the_good_ones(self):
        assert client_access.parse_extra_origins("*, https://ok.example, https://bad.example/x") == ["https://ok.example"]

    def test_wildcard_is_never_in_the_effective_list(self):
        assert "*" not in client_access.allowed_origins(raw_extra="*,https://ok.example")

    def test_an_over_long_entry_is_ignored(self):
        assert client_access.parse_extra_origins("https://" + "a" * 300 + ".example") == []

    def test_environment_variable_is_read_when_no_argument_is_given(self, monkeypatch):
        monkeypatch.setenv("CORS_EXTRA_ORIGINS", "https://env.example")
        assert client_access.allowed_origins() == [ANDROID_ORIGIN, "https://env.example"]


# ---------------------------------------------------------------- CORS behaviour, real app
class TestCorsOnTheRealApp:
    def test_preflight_from_the_android_origin_is_allowed(self, client):
        r = _preflight(client, ANDROID_ORIGIN)
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == ANDROID_ORIGIN
        allowed = r.headers["access-control-allow-headers"].lower()
        for h in ("authorization", "content-type", "x-client-version"):
            assert h in allowed
        methods = r.headers["access-control-allow-methods"]
        for m in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            assert m in methods
        assert r.headers["access-control-max-age"] == str(client_access.CORS_PREFLIGHT_MAX_AGE_SECONDS)

    def test_credentials_are_not_allowed(self, client):
        r = _preflight(client, ANDROID_ORIGIN)
        assert "access-control-allow-credentials" not in r.headers
        r2 = client.get("/healthz", headers={"Origin": ANDROID_ORIGIN})
        assert "access-control-allow-credentials" not in r2.headers

    def test_preflight_from_another_origin_is_refused(self, client):
        r = _preflight(client, EVIL_ORIGIN)
        assert r.status_code == 400
        assert "access-control-allow-origin" not in r.headers

    def test_an_actual_request_from_another_origin_gets_no_allow_header(self, client):
        r = client.get("/healthz", headers={"Origin": EVIL_ORIGIN})
        assert "access-control-allow-origin" not in r.headers

    def test_the_wildcard_is_never_returned(self, client):
        for origin in (ANDROID_ORIGIN, EVIL_ORIGIN, "null"):
            r = client.get("/healthz", headers={"Origin": origin})
            assert r.headers.get("access-control-allow-origin") != "*"

    def test_actual_response_exposes_the_headers_the_frontend_reads(self, client):
        r = client.get("/healthz", headers={"Origin": ANDROID_ORIGIN})
        assert r.headers["access-control-allow-origin"] == ANDROID_ORIGIN
        exposed = r.headers["access-control-expose-headers"]
        assert "X-Request-ID" in exposed and "Retry-After" in exposed

    def test_same_origin_web_requests_are_unchanged(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert not any(k.startswith("access-control-") for k in r.headers)

    def test_an_auth_error_still_carries_cors_so_the_app_can_read_it(self, client):
        r = client.get("/api/profile", headers={"Origin": ANDROID_ORIGIN})
        assert r.status_code == 401
        assert r.headers["access-control-allow-origin"] == ANDROID_ORIGIN

    def test_preflight_does_not_need_authentication(self, client, monkeypatch):
        async def _boom(*a, **k):
            raise AssertionError("a CORS preflight must never reach authentication")
        monkeypatch.setattr(main, "_verify_supabase_token", _boom)
        assert _preflight(client, ANDROID_ORIGIN, path="/api/notes", method="GET").status_code == 200

    def test_extra_origins_work_on_a_freshly_built_app(self):
        app = FastAPI()

        @app.get("/ping")
        async def ping():
            return {"ok": True}

        client_access.install_cors(app, client_access.allowed_origins(raw_extra="https://app.example.com"))
        c = TestClient(app)
        assert c.get("/ping", headers={"Origin": "https://app.example.com"}).headers["access-control-allow-origin"] == "https://app.example.com"
        assert c.get("/ping", headers={"Origin": ANDROID_ORIGIN}).headers["access-control-allow-origin"] == ANDROID_ORIGIN
        assert "access-control-allow-origin" not in c.get("/ping", headers={"Origin": EVIL_ORIGIN}).headers


# ---------------------------------------------------------------- version parsing
class TestVersionParsing:
    @pytest.mark.parametrize("value,expected", [
        ("1.2.3", (1, 2, 3)), ("0.0.0", (0, 0, 0)), ("10.20.30", (10, 20, 30)), (" 1.0.0 ", (1, 0, 0)),
    ])
    def test_valid_versions(self, value, expected):
        assert client_access.parse_version(value) == expected

    @pytest.mark.parametrize("value", ["", None, "1", "1.2", "1.2.3.4", "1.2.x", "v1.2.3", "1.2.3-beta", "-1.2.3", "12345.0.0", 5, [], "1..2"])
    def test_invalid_versions(self, value):
        assert client_access.parse_version(value) is None

    def test_numeric_not_textual_comparison(self):
        assert client_access.parse_version("1.10.0") > client_access.parse_version("1.2.0")
        assert client_access.parse_version("2.0.0") > client_access.parse_version("1.99.99")

    @pytest.mark.parametrize("value,expected", [
        ("android/1.2.3", ("android", (1, 2, 3))), ("web/0.0.1", ("web", (0, 0, 1))), ("ios/3.0.0", ("ios", (3, 0, 0))),
        ("android/1.2.3 ", ("android", (1, 2, 3))),  # surrounding whitespace is trimmed, as HTTP servers do
    ])
    def test_valid_client_versions(self, value, expected):
        assert client_access.parse_client_version(value) == expected

    @pytest.mark.parametrize("value", [
        "", None, "android", "android/", "android/1.2", "android/1.2.3.4", "Android/1.2.3", "android /1.2.3", "android/ 1.2.3",
        "android/1.2.3; DROP", "/1.2.3", "a/1.2.3", "android/" + "9" * 60, "x" * 200,
    ])
    def test_invalid_client_versions_are_ignored(self, value):
        assert client_access.parse_client_version(value) is None

    def test_format_round_trip(self):
        assert client_access.format_version((1, 2, 3)) == "1.2.3"


# ---------------------------------------------------------------- the gate on the real app
@pytest.fixture
def gated(client, monkeypatch):
    """MIN_ANDROID_VERSION=1.2.0 and a tripwire that fails the test if authentication runs for a refused request."""
    monkeypatch.setenv("MIN_ANDROID_VERSION", "1.2.0")
    calls = []

    async def _tripwire(authorization):
        calls.append(authorization)
        raise AssertionError("authentication ran for a request that should have been refused first")

    monkeypatch.setattr(main, "_verify_supabase_token", _tripwire)
    client.auth_calls = calls
    return client


class TestVersionGate:
    def test_inert_by_default(self, client):
        for header in ("android/0.0.1", "android/9.9.9", "web/0.0.1"):
            r = client.get("/api/profile", headers={"X-Client-Version": header})
            assert r.status_code == 401, header  # reached the normal auth check, was not refused

    def test_an_older_android_build_is_refused_with_app_update_required(self, gated):
        r = gated.get("/api/profile", headers={"X-Client-Version": "android/1.1.9"})
        assert r.status_code == REFUSAL_STATUS == 400
        assert _is_refused(r)
        body = r.json()
        assert body["detail"]["code"] == "APP_UPDATE_REQUIRED"
        assert body["detail"]["min_version"] == "1.2.0"
        assert body["detail"]["message"]
        assert r.headers["cache-control"] == "no-store"
        assert gated.auth_calls == []

    def test_the_refusal_is_not_a_426_and_claims_no_protocol_upgrade(self, gated):
        r = gated.get("/api/profile", headers={"X-Client-Version": "android/1.1.9"})
        assert r.status_code != 426
        assert "upgrade" not in {k.lower() for k in r.headers}

    def test_an_ordinary_error_is_not_mistaken_for_a_refusal(self, client):
        # The refusal shares status 400 with other errors, so the code is what identifies it.
        for r in (client.get("/api/profile"), client.post("/api/chat"), client.get("/healthz")):
            assert not _is_refused(r)

    def test_refusal_happens_before_validation_and_auth_on_every_api_method(self, gated):
        h = {"X-Client-Version": "android/1.0.0"}
        assert _is_refused(gated.post("/api/chat/stream", headers=h))
        assert _is_refused(gated.post("/api/chat", headers=h))
        assert _is_refused(gated.post("/api/notes", headers=h, json={}))
        assert _is_refused(gated.patch("/api/notes/abc", headers=h, json={}))
        assert _is_refused(gated.delete("/api/share", headers=h))
        assert _is_refused(gated.put("/api/profile", headers=h, json={}))
        assert _is_refused(gated.get("/api/shared/sometoken", headers=h))
        assert gated.auth_calls == []

    @pytest.mark.parametrize("version", ["android/1.2.0", "android/1.2.1", "android/1.10.0", "android/2.0.0", "android/99.0.0"])
    def test_the_minimum_and_newer_builds_are_not_refused(self, client, monkeypatch, version):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "1.2.0")
        r = client.get("/api/profile", headers={"X-Client-Version": version})
        assert r.status_code == 401 and not _is_refused(r)  # the normal auth error, not the refusal

    def test_no_header_is_never_refused(self, client, monkeypatch):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "9.9.9")
        assert client.get("/api/profile").status_code == 401

    @pytest.mark.parametrize("header", ["garbage", "android", "android/1.2", "android/1.2.3.4", "Android/0.0.1", "", "x" * 500])
    def test_an_unreadable_header_fails_open(self, client, monkeypatch, header):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "9.9.9")
        assert client.get("/api/profile", headers={"X-Client-Version": header}).status_code == 401

    def test_other_platforms_are_not_gated_by_the_android_minimum(self, client, monkeypatch):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "9.9.9")
        for header in ("web/0.0.1", "ios/0.0.1"):
            assert client.get("/api/profile", headers={"X-Client-Version": header}).status_code == 401

    @pytest.mark.parametrize("path", [
        "/healthz", "/readyz", "/", "/api/app-config", "/static/css/tokens.css", "/favicon.ico", "/sw.js",
    ])
    def test_non_api_paths_and_app_config_stay_reachable_for_an_old_build(self, gated, path):
        r = gated.get(path, headers={"X-Client-Version": "android/0.0.1"})
        assert not _is_refused(r), path

    def test_the_public_share_page_is_not_gated(self, gated):
        r = gated.get("/s/sometoken", headers={"X-Client-Version": "android/0.0.1"})
        assert not _is_refused(r)

    def test_the_refusal_carries_cors_headers_for_the_android_origin(self, gated):
        r = gated.get("/api/profile", headers={"X-Client-Version": "android/1.0.0", "Origin": ANDROID_ORIGIN})
        assert _is_refused(r)
        assert r.headers["access-control-allow-origin"] == ANDROID_ORIGIN

    def test_a_preflight_from_an_old_build_is_still_answered(self, gated):
        r = _preflight(gated, ANDROID_ORIGIN, path="/api/chat/stream")
        assert r.status_code == 200

    def test_an_invalid_minimum_is_ignored_and_refuses_nobody(self, client, monkeypatch):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "banana")
        assert client_access.min_android_version() == (0, 0, 0)
        r = client.get("/api/profile", headers={"X-Client-Version": "android/0.0.1"})
        assert r.status_code == 401

    def test_a_bad_minimum_is_warned_about_once_not_per_request(self, client, monkeypatch, caplog):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "banana")
        with caplog.at_level("WARNING", logger="kognit.client_access"):
            for _ in range(5):
                client.get("/api/profile", headers={"X-Client-Version": "android/0.0.1"})
        assert len([r for r in caplog.records if "MIN_ANDROID_VERSION" in r.getMessage()]) == 1

    def test_the_header_value_is_never_logged(self, gated, caplog):
        with caplog.at_level("DEBUG"):
            gated.get("/api/profile", headers={"X-Client-Version": "android/1.0.0"})
        assert "android/1.0.0" not in caplog.text


class TestGateAsgiBehaviour:
    """The gate is a pure ASGI middleware: it must pass every non-HTTP scope straight through."""

    def _run(self, scope):
        seen = []

        async def inner(s, receive, send):
            seen.append(s["type"])

        gate = client_access.ClientVersionGate(inner)

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            seen.append(("sent", message["type"]))

        asyncio.run(gate(scope, receive, send))
        return seen

    def test_lifespan_scope_passes_through(self):
        assert self._run({"type": "lifespan"}) == ["lifespan"]

    def test_websocket_scope_passes_through(self):
        assert self._run({"type": "websocket", "path": "/api/x", "headers": [(b"x-client-version", b"android/0.0.1")]}) == ["websocket"]

    def test_a_non_refused_http_request_reaches_the_inner_app_untouched(self, monkeypatch):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "1.0.0")
        scope = {"type": "http", "method": "POST", "path": "/api/chat", "headers": [(b"x-client-version", b"android/1.0.0")]}
        assert self._run(scope) == ["http"]

    def test_a_refused_request_never_reaches_the_inner_app(self, monkeypatch):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "1.0.0")
        scope = {"type": "http", "method": "POST", "path": "/api/chat", "headers": [(b"x-client-version", b"android/0.9.0")]}
        seen = self._run(scope)
        assert "http" not in seen
        assert ("sent", "http.response.start") in seen


# ---------------------------------------------------------------- /api/app-config
class TestAppConfig:
    def test_default_shape_and_values(self, client):
        r = client.get("/api/app-config")
        assert r.status_code == 200
        assert r.json() == {"api_version": 1, "android": {"min_version": "0.0.0", "latest_version": None}}

    def test_it_needs_no_authentication(self, client, monkeypatch):
        async def _boom(*a, **k):
            raise AssertionError("app-config must be public")
        monkeypatch.setattr(main, "_verify_supabase_token", _boom)
        assert client.get("/api/app-config").status_code == 200

    def test_environment_values_are_reflected(self, client, monkeypatch):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "1.2.0")
        monkeypatch.setenv("LATEST_ANDROID_VERSION", "1.10.4")
        assert client.get("/api/app-config").json()["android"] == {"min_version": "1.2.0", "latest_version": "1.10.4"}

    def test_latest_is_never_reported_below_the_minimum(self, client, monkeypatch):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "2.0.0")
        monkeypatch.setenv("LATEST_ANDROID_VERSION", "1.0.0")
        assert client.get("/api/app-config").json()["android"] == {"min_version": "2.0.0", "latest_version": "2.0.0"}

    def test_invalid_values_fall_back_to_the_defaults(self, client, monkeypatch):
        monkeypatch.setenv("MIN_ANDROID_VERSION", "soon")
        monkeypatch.setenv("LATEST_ANDROID_VERSION", "new")
        assert client.get("/api/app-config").json()["android"] == {"min_version": "0.0.0", "latest_version": None}

    def test_it_is_reachable_by_an_outdated_build(self, gated):
        r = gated.get("/api/app-config", headers={"X-Client-Version": "android/0.0.1"})
        assert r.status_code == 200
        assert r.json()["android"]["min_version"] == "1.2.0"

    def test_it_is_not_cached_and_carries_cors_for_the_android_origin(self, client):
        r = client.get("/api/app-config", headers={"Origin": ANDROID_ORIGIN})
        assert r.headers["cache-control"] == "no-cache"
        assert r.headers["access-control-allow-origin"] == ANDROID_ORIGIN

    def test_it_exposes_only_version_policy_no_secrets_or_environment(self, client, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
        body = client.get("/api/app-config").text
        assert "super-secret-value" not in body
        assert set(client.get("/api/app-config").json()) == {"api_version", "android"}
        assert set(client.get("/api/app-config").json()["android"]) == {"min_version", "latest_version"}


# ---------------------------------------------------------------- wiring
class TestWiring:
    def test_the_gate_sits_inside_cors_so_the_refusal_is_readable_cross_origin(self, gated):
        r = gated.post("/api/chat", headers={"X-Client-Version": "android/0.0.1", "Origin": ANDROID_ORIGIN})
        assert _is_refused(r)
        assert r.headers["access-control-allow-origin"] == ANDROID_ORIGIN

    def test_existing_public_endpoints_still_work(self, client):
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").status_code in (200, 503)
        assert client.get("/").status_code == 200