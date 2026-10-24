"""
Payment-verification boundary for Pro activation (Pre-Phase 10 remediation).

STATUS: BOUNDARY ONLY - NO LIVE bKASH INTEGRATION EXISTS.
-------------------------------------------------------
This module defines WHO is allowed to turn a payment into a Pro subscription
and what must be proven first. It deliberately contains no bKash API calls:
the bKash Merchant request/response formats, credentials and sandbox are not
available in this repository or environment, and inventing them would be
fabricating an integration. A real verifier must be written against bKash's
published API once credentials are provided, and must satisfy the
PaymentVerifier protocol below.

THE RULES THIS BOUNDARY ENFORCES
--------------------------------
1. A client-supplied "payment succeeded" flag, a screenshot, or a bare
   transaction reference is never enough. Only a PaymentVerifier - code that
   asks the payment provider server-to-server - may produce a VerifiedPayment.
2. The verified payment must belong to the user being upgraded, be in BDT,
   name a known billing period, and match the approved price exactly.
3. Activation is performed by a database function that only the service_role
   can execute (public.activate_pro_subscription, migration 0008). The request
   path of the app never holds a service-role key; the activation gateway is a
   separate, deliberately small component that does.
4. Activation is idempotent on the provider's payment id, so a replayed
   callback cannot grant a second period.
5. Renewal is MANUAL (no auto-renew). A renewal while Pro is still active
   extends from the current expiry; an expired subscription simply stops
   matching and the user is Free again, with payment history untouched.

PRICES are a CEO decision (299 BDT monthly, 3,199 BDT yearly). The database
table public.plan_prices is the authority at activation time;
PRO_PRICES_BDT below exists so a mismatch is rejected early, and
test_payments_boundary.py asserts the two stay identical.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol

logger = logging.getLogger("kognit.payments")

PROVIDER_BKASH = "bkash"
CURRENCY_BDT = "BDT"
BILLING_MONTHLY = "monthly"
BILLING_YEARLY = "yearly"
PRO_PRICES_BDT = {BILLING_MONTHLY: 299, BILLING_YEARLY: 3199}


class PaymentVerificationError(Exception):
    """The payment could not be verified; no subscription may be activated."""


@dataclass(frozen=True)
class VerifiedPayment:
    provider: str
    provider_payment_id: str
    user_id: str
    amount_bdt: int
    currency: str
    billing_period: str
    verified_at: datetime


class PaymentVerifier(Protocol):
    def verify(self, provider_payment_id: str, expected_user_id: str) -> Optional[VerifiedPayment]:
        """Ask the PROVIDER whether this payment completed. Returns None if it
        did not (unknown id, pending, failed, refunded, reversed)."""


class ActivationGateway(Protocol):
    def activate(self, payment: VerifiedPayment) -> str:
        """Creates/extends the subscription for a VERIFIED payment and returns
        the subscription id. Must be idempotent on provider_payment_id."""


def validate_verified_payment(payment: VerifiedPayment, expected_user_id: str) -> None:
    """Independent re-check of what a verifier returned (defence in depth: a
    buggy or compromised verifier must not be able to activate the wrong user
    or the wrong amount)."""
    if payment.user_id != expected_user_id:
        raise PaymentVerificationError("payment belongs to a different user")
    if payment.currency != CURRENCY_BDT:
        raise PaymentVerificationError("unsupported currency")
    expected_amount = PRO_PRICES_BDT.get(payment.billing_period)
    if expected_amount is None:
        raise PaymentVerificationError("unknown billing period")
    if payment.amount_bdt != expected_amount:
        raise PaymentVerificationError("amount does not match the approved price")
    if not payment.provider_payment_id or not payment.provider_payment_id.strip():
        raise PaymentVerificationError("missing provider payment id")


def activate_pro_from_payment(
    verifier: PaymentVerifier,
    gateway: ActivationGateway,
    provider_payment_id: str,
    user_id: str,
) -> str:
    """The only supported path from "the user says they paid" to Pro.
    Returns the subscription id. Raises PaymentVerificationError on any doubt."""
    if not provider_payment_id or not provider_payment_id.strip():
        raise PaymentVerificationError("missing provider payment id")
    try:
        payment = verifier.verify(provider_payment_id.strip(), user_id)
    except PaymentVerificationError:
        raise
    except Exception as exc:  # noqa: BLE001 - a verifier failure must never activate
        logger.error("payment verifier failed type=%s", type(exc).__name__)
        raise PaymentVerificationError("verification unavailable") from exc
    if payment is None:
        raise PaymentVerificationError("payment not confirmed by the provider")
    validate_verified_payment(payment, user_id)
    return gateway.activate(payment)


class UnconfiguredVerifier:
    """Default when no real verifier exists: refuses everything."""

    def verify(self, provider_payment_id: str, expected_user_id: str) -> Optional[VerifiedPayment]:
        raise PaymentVerificationError("no payment provider is configured")