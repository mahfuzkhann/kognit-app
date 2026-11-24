"""
Client access for NON-BROWSER-ORIGIN clients: the bundled Android app (Capacitor).

Why this module exists (Report 2, X-06 and X-28)
------------------------------------------------
The website is served by this same server, so its API calls are same-origin and
need no CORS. The Android app is different: Capacitor serves the bundled frontend
from the device itself, so every API call is CROSS-ORIGIN and the browser engine
inside the app blocks it unless this server explicitly allows that origin. Also,
an installed app cannot be updated by us, so the server needs a way to tell an
old, broken build "please update".

What it provides - nothing more
-------------------------------
1. CORS allow-list (`install_cors`)
     * Always allows the Capacitor Android origin `https://localhost`.
     * Extra origins can be added with the CORS_EXTRA_ORIGINS environment variable
       (comma separated, exact origins only). A wildcard is never accepted.
     * The API authenticates with an Authorization header, never with cookies, so
       credentials are NOT allowed cross-origin.
     * `X-Request-ID` and `Retry-After` are exposed because the frontend reads
       them (Retry-After already drives the "try again in N seconds" message).
2. Client version gate (`ClientVersionGate`)
     * A client may send `X-Client-Version: android/1.2.3`.
     * If MIN_ANDROID_VERSION is set and that client is older, any /api/ request
       is answered HTTP 400 with {"detail": {"code": "APP_UPDATE_REQUIRED", ...}}
       BEFORE any authentication, database or AI work happens. The app must act on
       `detail.code`, never on the status number alone (see APP_UPDATE_REQUIRED_STATUS).
     * INERT by default: no variable set = nothing is ever refused. A request
       with no header (the website, curl, tests) or an unreadable header is
       never refused. The gate fails OPEN on purpose: a malformed header must not
       be able to lock students out.
     * It is a pure ASGI middleware, not BaseHTTPMiddleware, so streaming
       responses, client-disconnect detection ("Stop generating") and background
       tasks are not touched.
3. GET /api/app-config (public, no secrets)
     * Lets the app learn on launch which version is the minimum and which is the
       latest, so it can show an update screen without making a chat request.

Security / privacy notes
------------------------
* Nothing here trusts the version header for anything except deciding to REFUSE
  an outdated client. It never grants access. A client can lie about its version
  to dodge the gate; that only matters for retiring broken builds, and every
  protected route still does its own authentication and authorisation.
* The header value is never logged.
* No new dependency: Starlette's CORSMiddleware ships with FastAPI.
"""
import logging
import os
import re
from typing import List, Optional, Tuple

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

logger = logging.getLogger("kognit.client_access")

# --------------------------------------------------------------------------- CORS
# Capacitor serves an Android app from https://localhost when `server.androidScheme` is "https".
# The Android project must PIN that scheme explicitly rather than rely on a default.
ANDROID_APP_ORIGIN = "https://localhost"

CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# Request headers the frontend sends: Authorization (every private call), Content-Type (JSON calls),
# X-Client-Version (this module). Content-Type is listed explicitly for clarity.
CORS_ALLOWED_HEADERS = ["Authorization", "Content-Type", "X-Client-Version"]
# Response headers JavaScript is allowed to read cross-origin. Retry-After is read by app.js / share.js.
CORS_EXPOSED_HEADERS = ["X-Request-ID", "Retry-After"]
CORS_PREFLIGHT_MAX_AGE_SECONDS = 600

# An origin is exactly scheme://host[:port]. No wildcard, no path, no query, no userinfo, no trailing slash.
_ORIGIN_RE = re.compile(r"^https?://[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:\d{1,5})?$")
_MAX_ORIGIN_CHARS = 253


def parse_extra_origins(raw: Optional[str]) -> List[str]:
    """Turns the CORS_EXTRA_ORIGINS value into a clean list of exact origins.

    Invalid entries (a wildcard, a path, a missing scheme, ...) are IGNORED with a warning, never
    "fixed" and never turned into something more permissive. An empty or missing value gives an empty list."""
    if raw is None:
        return []
    origins: List[str] = []
    for entry in str(raw).split(","):
        candidate = entry.strip().lower()
        if not candidate:
            continue
        if len(candidate) > _MAX_ORIGIN_CHARS or not _ORIGIN_RE.match(candidate):
            logger.warning("CORS_EXTRA_ORIGINS: ignoring an invalid entry %r (an exact origin such as https://example.com is required)",
                           candidate[:80])
            continue
        if candidate not in origins:
            origins.append(candidate)
    return origins


def allowed_origins(raw_extra: Optional[str] = None) -> List[str]:
    """The built-in Android origin first, then any valid extra origins. The built-in one can never be removed
    by configuration, so a typo in CORS_EXTRA_ORIGINS cannot silently break the Android app."""
    if raw_extra is None:
        raw_extra = os.getenv("CORS_EXTRA_ORIGINS")
    result = [ANDROID_APP_ORIGIN]
    for origin in parse_extra_origins(raw_extra):
        if origin not in result:
            result.append(origin)
    return result


def install_cors(app: FastAPI, origins: Optional[List[str]] = None) -> None:
    """Adds the CORS middleware. Must be installed LAST (= outermost) so that every response, including the
    update-required refusal from the version gate and 401/429/503 error responses, carries the CORS headers; otherwise the app
    could not read the error and would show a generic network failure instead."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins if origins is not None else allowed_origins(),
        allow_methods=CORS_ALLOWED_METHODS,
        allow_headers=CORS_ALLOWED_HEADERS,
        expose_headers=CORS_EXPOSED_HEADERS,
        allow_credentials=False,
        max_age=CORS_PREFLIGHT_MAX_AGE_SECONDS,
    )


# ------------------------------------------------------------------- version gate
CLIENT_VERSION_HEADER = b"x-client-version"
API_VERSION = 1
ENV_MIN_ANDROID_VERSION = "MIN_ANDROID_VERSION"
ENV_LATEST_ANDROID_VERSION = "LATEST_ANDROID_VERSION"
ERROR_APP_UPDATE_REQUIRED = "APP_UPDATE_REQUIRED"
# Status choice. 426 is NOT used: RFC 9110 requires an `Upgrade` header on every 426 and defines it for HTTP protocol
# upgrades, which an app-version refusal is not. 403 and 409 already have special meaning in the frontend (PLAN_REQUIRED /
# quota handling, sync conflicts) and 422 is FastAPI's request-validation status, so reusing any of them would be
# mis-handled by existing code. 400 has no special handling in the frontend, so the stable `detail.code` is the contract.
APP_UPDATE_REQUIRED_STATUS = 400

Version = Tuple[int, int, int]
_VERSION_RE = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
_CLIENT_VERSION_RE = re.compile(r"^([a-z]{2,16})/(\d{1,4}\.\d{1,4}\.\d{1,4})$")
_MAX_CLIENT_VERSION_CHARS = 40
_warned_bad_values = set()


def parse_version(value: Optional[str]) -> Optional[Version]:
    """'1.2.3' -> (1, 2, 3); anything else -> None. Compared as numbers, so 1.10.0 is newer than 1.2.0."""
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.match(value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def parse_client_version(value: Optional[str]) -> Optional[Tuple[str, Version]]:
    """'android/1.2.3' -> ('android', (1, 2, 3)); an absent, over-long or unreadable value -> None."""
    if not isinstance(value, str) or len(value) > _MAX_CLIENT_VERSION_CHARS:
        return None
    match = _CLIENT_VERSION_RE.match(value.strip())
    if not match:
        return None
    version = parse_version(match.group(2))
    if version is None:
        return None
    return match.group(1), version


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def _env_version(name: str) -> Optional[Version]:
    """A version from the environment, or None when unset or invalid (a bad value is warned about once and ignored,
    so a typo can never refuse every student)."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    parsed = parse_version(raw)
    if parsed is None and (name, raw) not in _warned_bad_values:
        _warned_bad_values.add((name, raw))
        logger.warning("%s=%r is not a version like 1.2.3 and is ignored", name, raw[:40])
    return parsed


def min_android_version() -> Version:
    """The oldest Android build still allowed. (0, 0, 0) = no restriction (the default)."""
    return _env_version(ENV_MIN_ANDROID_VERSION) or (0, 0, 0)


def latest_android_version() -> Optional[Version]:
    """The newest published Android build, for a soft 'update available' prompt. Never lower than the minimum."""
    latest = _env_version(ENV_LATEST_ANDROID_VERSION)
    if latest is None:
        return None
    return max(latest, min_android_version())


def _is_gated_path(path: str) -> bool:
    """Only the JSON API is gated. Pages, static files, health checks and app-config stay reachable, so an old
    app can still discover that it must update."""
    return path.startswith("/api/") and path != "/api/app-config"


def _client_version_from_scope(scope) -> Optional[Tuple[str, Version]]:
    for name, value in scope.get("headers") or ():
        if name == CLIENT_VERSION_HEADER:
            try:
                return parse_client_version(value.decode("latin-1"))
            except Exception:
                return None
    return None


class ClientVersionGate:
    """Pure ASGI middleware: refuses /api/ requests from an Android build older than MIN_ANDROID_VERSION."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") == "OPTIONS" or not _is_gated_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        client = _client_version_from_scope(scope)
        if client is not None and client[0] == "android":
            minimum = min_android_version()
            if client[1] < minimum:
                response = JSONResponse(
                    status_code=APP_UPDATE_REQUIRED_STATUS,
                    content={
                        "detail": {
                            "code": ERROR_APP_UPDATE_REQUIRED,
                            "message": "This version of Kloriz is no longer supported. Please update the app to continue.",
                            "min_version": format_version(minimum),
                        }
                    },
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


# ------------------------------------------------------------------- app-config
router = APIRouter()


def build_app_config() -> dict:
    latest = latest_android_version()
    return {
        "api_version": API_VERSION,
        "android": {
            "min_version": format_version(min_android_version()),
            "latest_version": format_version(latest) if latest is not None else None,
        },
    }


@router.get("/api/app-config")
async def app_config():
    """Public and secret-free: version policy only. No database, no AI, no student data."""
    return JSONResponse(content=build_app_config(), headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------- wiring
def install(app: FastAPI) -> None:
    """One call from main.py. ORDER MATTERS: the gate is added first (inner) and CORS last (outermost)."""
    app.include_router(router)
    app.add_middleware(ClientVersionGate)
    install_cors(app)