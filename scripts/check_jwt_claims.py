#!/usr/bin/env python3
"""
Prints what a Supabase access token says about WHEN the student signed in - without ever
printing the token itself. Use it on STAGING to check the assumption account deletion relies on
(docs/ACCOUNT_DELETION.md, "Staging check").

Usage (PowerShell, from the project root):
    python scripts/check_jwt_claims.py
Then paste the access token when asked and press Enter (the input is hidden). Nothing is saved,
sent or logged. Get a token from the browser: open DevTools > Application > Local Storage >
the sb-...-auth-token entry > "access_token". Use a TEST account only.

What it reports: which claims exist, every `amr` entry (method + age in seconds), whether the
account-deletion rule would call the login "recent", and the token's own age (iat). It prints no
email, no user id, no session id.
"""
import base64
import getpass
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import account_deletion as ad  # noqa: E402


def main() -> int:
    token = getpass.getpass("Paste the access token (hidden), then Enter: ").strip()
    claims = ad._payload(token)
    if claims is None:
        print("Could not read that as a JWT.")
        return 2
    now = time.time()
    print("\nClaim names present :", ", ".join(sorted(claims)))
    iat = claims.get("iat")
    if isinstance(iat, (int, float)):
        print(f"Token age (iat)      : {int(now - iat)} s   <- NOT used as login evidence (a refresh resets it)")
    amr = claims.get("amr")
    print("amr present          :", "yes" if "amr" in claims else "NO")
    if isinstance(amr, list):
        for i, e in enumerate(amr):
            if isinstance(e, dict):
                ts = e.get("timestamp")
                age = f"{int(now - ts)} s ago" if isinstance(ts, (int, float)) and not isinstance(ts, bool) else "no usable timestamp"
                counts = "COUNTS" if str(e.get("method", "")).lower() in ad.AUTH_METHODS else "ignored"
                print(f"  amr[{i}] method={e.get('method')!r:16} {age:22} {counts}")
            else:
                print(f"  amr[{i}] is a plain {type(e).__name__} (no timestamp) -> ignored")
    age = ad.login_age_seconds(token, now)
    window = ad.reauth_window_seconds()
    print("\nSign-in age used    :", "UNKNOWN (no qualifying amr entry)" if age is None else f"{int(age)} s")
    print("Window              :", window, "s")
    verdict = "RECENT - deletion would be allowed" if ad.login_is_recent(token, now) else "NOT RECENT - deletion would answer 403 REAUTH_REQUIRED"
    print("Verdict             :", verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())