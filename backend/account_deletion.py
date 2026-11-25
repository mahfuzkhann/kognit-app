"""
Kloriz account deletion - the ONLY module that touches the Supabase service-role key.

WHY ONE MODULE
  Deleting an auth user needs Supabase's admin API, which needs the service-role
  key - a credential that bypasses Row Level Security on EVERY table. It is kept
  in one small file so that:
    * it is easy to review in one sitting;
    * nothing else in the backend can import it by accident;
    * tests can prove it never reaches a log line, a response or a student's
      browser (test_account_deletion.py does).
  The key is read from the environment AT CALL TIME and is never stored in a
  module variable, never logged and never put in an exception message.

WHAT "DELETE ACCOUNT" DOES
  One call: DELETE {SUPABASE_URL}/auth/v1/admin/users/{user_id}. Every table that
  holds student data references auth.users(id) ON DELETE CASCADE (see
  docs/ACCOUNT_DELETION.md for the full inventory, and
  supabase/tests/account_deletion_cascade_test.sql, which fails if a table does
  not), so PostgreSQL removes ALL of the student's rows in the same transaction as
  the auth user: either everything is gone or nothing is. Supabase also removes
  the user's identities, sessions and refresh tokens. A still-unexpired access
  token stops working immediately, because the backend validates every request
  against Supabase Auth (which no longer knows the user).

WHAT IT DOES NOT DO
  * It does not delete anything for ANY OTHER user: the id comes from the
    verified access token, never from the request body.
  * It does not hold payment records back. payments cascades like everything
    else. Whether payment records must be RETAINED (tax / accounting) is a legal
    question for counsel - see docs/ACCOUNT_DELETION.md "Open legal questions".
    Payments are not live yet, so no real payment record can exist today.

RE-AUTHENTICATION ("recent login") - FAILS CLOSED
  Deleting an account is irreversible, so a stolen-but-still-valid access token
  must not be enough. The caller must have SIGNED IN within the last
  REAUTH_WINDOW_SECONDS (default 600, set with ACCOUNT_DELETE_REAUTH_WINDOW_SECONDS,
  allowed range 30-900).

  The evidence is Supabase's `amr` claim: a list of {method, timestamp} entries,
  one per authentication method used. ONLY entries for an interactive sign-in
  method count (AUTH_METHODS below). Deliberately NOT evidence:
    * `iat` (issued-at): a token REFRESH issues a new token with a new `iat`
      without the student having signed in again, so `iat` proves nothing;
    * an `amr` entry whose method is `token_refresh` (Supabase documents it as a
      possible method value): it records a refresh, not a login;
    * `anonymous`, `email_change` and any method this code does not know;
    * an `amr` that is a plain list of strings (no timestamps), or missing.
  If no qualifying entry with a usable timestamp exists, the answer is "not
  recent" and the endpoint refuses (403 REAUTH_REQUIRED). It never guesses.

  Reading the claims is safe because the token has ALREADY been verified by
  Supabase Auth in get_current_user_and_token(); this module only reads the
  payload, it never trusts an unverified token on its own. No password is ever
  sent to or handled by this backend.

  WHAT IS NOT PROVEN OFFLINE: how a real Supabase project fills `amr` after a
  token refresh. docs/ACCOUNT_DELETION.md "Staging check" makes the operator
  prove it with scripts/check_jwt_claims.py before deletion is enabled.
"""

import base64
import json
import logging
import os
import re
import time
import uuid
from typing import Optional

import httpx

from backend import http_client

logger = logging.getLogger("kognit.account_deletion")

SERVICE_ROLE_ENV = "SUPABASE_SERVICE_ROLE_KEY"
CONFIRMATION_WORD = "DELETE"
# A sign-in older than the window must be repeated before deleting. The default 10
# minutes is long enough to read the dialog and short enough that a borrowed,
# unlocked device or a leaked token is not enough.
REAUTH_WINDOW_ENV = "ACCOUNT_DELETE_REAUTH_WINDOW_SECONDS"
REAUTH_WINDOW_DEFAULT_SECONDS = 10 * 60
REAUTH_WINDOW_MIN_SECONDS = 30
REAUTH_WINDOW_MAX_SECONDS = 15 * 60
# Methods that mean "the student really signed in". Anything else in `amr` (notably
# token_refresh, anonymous, email_change) is ignored on purpose - see the module docstring.
AUTH_METHODS = frozenset({
    "password", "oauth", "otp", "magiclink", "recovery", "sso/saml", "email/signup",
    "mfa/totp", "mfa/phone", "mfa/webauthn",
})
MAX_REQUEST_BODY_BYTES = 1024
ADMIN_TIMEOUT_SECONDS = 15.0

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class AccountDeletionUnavailable(Exception):
    """The server is not configured to delete accounts (key or URL missing)."""


class AccountDeletionFailed(Exception):
    """Supabase refused or could not be reached. Nothing was deleted (the
    cascade is one transaction), so the student can safely try again."""


def confirmation_is_valid(value) -> bool:
    return isinstance(value, str) and value.strip().upper() == CONFIRMATION_WORD


def _payload(token: str) -> Optional[dict]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def reauth_window_seconds() -> int:
    """The configured window, clamped to 30-900 s. A bad value falls back to the
    default - it can never silently widen the window past the maximum."""
    raw = os.getenv(REAUTH_WINDOW_ENV, "").strip()
    if not raw:
        return REAUTH_WINDOW_DEFAULT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return REAUTH_WINDOW_DEFAULT_SECONDS
    return max(REAUTH_WINDOW_MIN_SECONDS, min(REAUTH_WINDOW_MAX_SECONDS, value))


def login_age_seconds(token: str, now: Optional[float] = None) -> Optional[float]:
    """Seconds since the student last SIGNED IN, or None when there is no usable
    evidence (None means 'not recent' - the caller must fail closed)."""
    claims = _payload(token or "")
    if claims is None:
        return None
    amr = claims.get("amr")
    if not isinstance(amr, list):
        return None
    stamps = []
    for entry in amr:
        if not isinstance(entry, dict):
            continue  # plain-string amr entries carry no timestamp
        method = entry.get("method")
        ts = entry.get("timestamp")
        if not isinstance(method, str) or method.strip().lower() not in AUTH_METHODS:
            continue
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
            continue
        stamps.append(float(ts))
    if not stamps:
        return None
    current = time.time() if now is None else now
    age = current - max(stamps)
    # A timestamp in the future is a clock problem or tampering, not "recent".
    return age if age >= -60 else None


def login_is_recent(token: str, now: Optional[float] = None) -> bool:
    age = login_age_seconds(token, now)
    return age is not None and age <= reauth_window_seconds()


async def delete_auth_user(user_id: str) -> str:
    """
    Permanently deletes the Supabase auth user and, through ON DELETE CASCADE,
    every row they own. Returns "deleted" or "already_deleted" (idempotent: a
    retry after a lost response is a success, not an error).

    Raises AccountDeletionUnavailable (not configured) or AccountDeletionFailed.
    Error messages are fixed strings: no response body, header or key in them.
    """
    base_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv(SERVICE_ROLE_ENV, "")
    if not base_url or not service_key:
        raise AccountDeletionUnavailable("account deletion is not configured")

    # The id came from a verified token, but it is about to become part of a URL
    # path, so it must be exactly a UUID - nothing else can ever be interpolated.
    try:
        canonical = str(uuid.UUID(str(user_id)))
    except (ValueError, AttributeError, TypeError):
        raise AccountDeletionFailed("invalid account identifier")
    if not _UUID_RE.match(canonical):
        raise AccountDeletionFailed("invalid account identifier")

    try:
        async with http_client.client(timeout=ADMIN_TIMEOUT_SECONDS) as client:
            # client.delete() has no body parameter in httpx, so use request().
            resp = await client.request(
                "DELETE",
                f"{base_url}/auth/v1/admin/users/{canonical}",
                headers={
                    "apikey": service_key,
                    "Authorization": f"Bearer {service_key}",
                    "Content-Type": "application/json",
                },
                # Hard delete: a soft-deleted user would keep their data.
                json={"should_soft_delete": False},
            )
    except httpx.RequestError:
        logger.error("account deletion: network error talking to Supabase Auth")
        raise AccountDeletionFailed("could not reach the authentication service")

    if resp.status_code in (200, 204):
        return "deleted"
    if resp.status_code == 404:
        return "already_deleted"
    if resp.status_code in (401, 403):
        # The service-role key is wrong or revoked: an operator problem, never the
        # student's. Status only - the response body is not logged.
        logger.error("account deletion: Supabase rejected the service-role key (status=%d)", resp.status_code)
        raise AccountDeletionUnavailable("account deletion is not configured correctly")
    logger.error("account deletion: Supabase Auth returned status=%d", resp.status_code)
    raise AccountDeletionFailed("the authentication service could not delete the account")