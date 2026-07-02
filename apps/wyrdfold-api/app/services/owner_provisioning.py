"""First-run owner provisioning for self-hosted deployments (Phase 2).

The plan (docs/plan-wyrdfold-deployment-modes.md): in ``self_host`` mode the
instance has exactly one account unless the operator opens it, and that owner
should exist after "set two env vars and boot" — no Supabase-dashboard
spelunking. At startup, when ``DEPLOYMENT_MODE=self_host`` and ``OWNER_EMAIL``
is set, we idempotently create that auth user (email-confirmed) via the admin
API; the owner then signs in through the normal magic-link flow, gets a JWT,
and RLS applies to them like any user.

Failure posture: warn loudly and continue, don't block boot. A deterministic
misconfig (bad service key) already fails earlier gates; what reaches here can
be transient (GoTrue still warming in a compose race), and blocking every boot
on it would turn a retryable blip into an outage. The operator watching a
first-run deploy log sees exactly what happened either way.
"""

from __future__ import annotations

import logging

from supabase import Client

from app.config import Settings

logger = logging.getLogger("app")


def provision_owner(supabase: Client, s: Settings) -> None:
    """Idempotently ensure the self-host owner's auth user exists.

    No-op unless ``deployment_mode == "self_host"`` AND ``owner_email`` is set.
    """
    if s.deployment_mode != "self_host":
        return
    if not s.owner_email:
        # Not fatal: pre-Phase-2 deployments (and the current prod) provision
        # accounts via the invite flow instead. Surface the option once.
        logger.info(
            "self_host mode with no OWNER_EMAIL set — skipping owner "
            "provisioning. Set OWNER_EMAIL to auto-create the owner account "
            "on boot (sign-in stays magic-link)."
        )
        return

    email = s.owner_email.strip().lower()
    try:
        supabase.auth.admin.create_user(
            {"email": email, "email_confirm": True}
        )
    except Exception as exc:
        message = str(exc)
        if "already" in message.lower() and (
            "registered" in message.lower() or "exists" in message.lower()
        ):
            logger.info("owner_provisioning: %s already exists — ok.", email)
            return
        logger.warning(
            "owner_provisioning: could not create %s (%s). The app is up, but "
            "the owner cannot sign in until this account exists — fix the "
            "cause and reboot, or create the user via the Supabase dashboard.",
            email,
            message,
        )
        return
    # WARNING on purpose: under the default text log config, root has no
    # handler and stdlib's lastResort prints WARNING+ only — INFO would make
    # the single most important first-run message invisible in the deploy
    # log. This fires once per instance lifetime.
    logger.warning(
        "owner_provisioning: created owner %s — sign in via the magic-link "
        "form to get started.",
        email,
    )
