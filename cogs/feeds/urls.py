"""Normalizing the configured DOMAIN into something urlsplit can parse.

The switch_shop_info command this replaces rendered DOMAIN as a bare
**Host:** field, so deployments configured it without a scheme. Left alone,
urlsplit puts a scheme-less string entirely in `path`: the embed would drop
the host from its URLs and the probe would request an address that does not
exist, reporting UNKNOWN forever.
"""


def with_scheme(domain: str) -> str:
    """A base URL with a scheme, whatever DOMAIN was configured as."""
    base = (domain or "").strip().rstrip("/")
    if base and "://" not in base:
        base = f"https://{base}"
    return base
