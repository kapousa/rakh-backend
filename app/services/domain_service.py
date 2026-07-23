"""
Custom domain verification.

An agency on the Enterprise plan can point their own domain (e.g.
reports.blackrockmedia.com) at RAKH for full white-label reselling. Before
we treat that domain as theirs — and critically, before we let a reverse
proxy auto-provision a TLS certificate for it (see the Caddy on-demand-TLS
"ask" endpoint in routers/public.py) — we verify they actually control it
via a real DNS lookup, not just trust whatever string they typed into a
settings field.

Verification method: CNAME record pointing at CUSTOM_DOMAIN_CNAME_TARGET.
This is the standard approach (same one Vercel/Netlify/Cloudflare Pages
use for custom domains) — the agency's DNS provider needs a CNAME record:

    reports.blackrockmedia.com.  CNAME  app.rakh.io.

Root/apex domains (e.g. "blackrockmedia.com" with no subdomain) often can't
use a CNAME per DNS spec — we fall back to checking for an ALIAS/ANAME
record where supported, or the agency should use a subdomain instead
(this is a real DNS limitation, not a RAKH one, and matches how every
major platform handles this — recommend a subdomain in the UI copy).
"""
from __future__ import annotations

import dns.resolver

from app.core.config import get_settings


def _normalize(hostname: str) -> str:
    return hostname.strip().lower().rstrip(".")


def verify_domain(domain: str) -> dict:
    """
    Returns:
        {"verified": bool, "found": str | None, "expected": str, "error": str | None}
    Never raises — DNS lookups fail in all sorts of normal, non-exceptional
    ways (NXDOMAIN, timeout, no CNAME present yet because propagation is
    still in progress), and the caller just needs a clean yes/no plus a
    human-readable reason to show in the UI.
    """
    settings = get_settings()
    expected = _normalize(settings.CUSTOM_DOMAIN_CNAME_TARGET)
    domain = _normalize(domain)

    try:
        answers = dns.resolver.resolve(domain, "CNAME", lifetime=5.0)
        found = _normalize(str(answers[0].target))
        if found == expected:
            return {"verified": True, "found": found, "expected": expected, "error": None}
        return {
            "verified": False,
            "found": found,
            "expected": expected,
            "error": f"Found a CNAME record, but it points to '{found}' instead of '{expected}'.",
        }
    except dns.resolver.NXDOMAIN:
        return {"verified": False, "found": None, "expected": expected, "error": "Domain does not exist (NXDOMAIN)."}
    except dns.resolver.NoAnswer:
        return {
            "verified": False,
            "found": None,
            "expected": expected,
            "error": "No CNAME record found for this domain yet — DNS changes can take up to 24-48h to propagate.",
        }
    except dns.exception.Timeout:
        return {"verified": False, "found": None, "expected": expected, "error": "DNS lookup timed out. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 — surface anything unexpected as a readable message, don't 500
        return {"verified": False, "found": None, "expected": expected, "error": f"DNS lookup failed: {exc}"}


def is_domain_verified_for_agency(sb, domain: str) -> bool:
    """
    Used by the Caddy on-demand-TLS 'ask' callback (routers/public.py) —
    checks our own database record rather than re-doing a live DNS lookup
    on every TLS handshake, since verification already happened via the
    explicit verify_domain() flow above and is stored on the agency row.
    """
    domain = _normalize(domain)
    res = (
        sb.table("agencies")
        .select("id")
        .eq("custom_domain", domain)
        .eq("custom_domain_verified", True)
        .eq("white_label_enabled", True)
        .execute()
    )
    return bool(res.data)
