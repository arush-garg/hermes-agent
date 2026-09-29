"""Local-connectivity probing for the connectivity-pause feature.

When a model call fails at the transport layer the turn loop must decide: did the
user's Wi-Fi die (pause and wait for it to come back) or did the provider go down
(fail over as usual)? ``classify_transport_failure`` sorts the exception;
``probe_connectivity`` then actively dials the provider host plus neutral internet
endpoints to confirm which side is dark.

Pure stdlib (``socket``); safe to call from the turn loop. Probes never raise and
never log above debug — an unreachable host is an expected outcome here, not an
error.
"""

from __future__ import annotations

import errno
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Failure kinds returned by classify_transport_failure. Only "dns" / "tcp" /
# "timeout" are connectivity-loss candidates; everything else keeps the normal
# retry/failover path.
DNS = "dns"
TCP = "tcp"
TIMEOUT = "timeout"
TLS_CERT = "tls_cert"
HTTP = "http"
OTHER = "other"

__all__ = [
    "DNS", "TCP", "TIMEOUT", "TLS_CERT", "HTTP", "OTHER",
    "ProbeOutcome", "classify_transport_failure", "probe_connectivity",
    "provider_host_for",
]

# Carries an HTTP response: auth failures, billing/quota, rate limits, policy
# rejections, and explicit status codes. None of these implicate the local link.
_HTTP_MESSAGE_PATTERNS = (
    "unauthorized", "forbidden", "invalid api key", "invalid_api_key",
    "incorrect api key", "authentication failed", "access denied",
    "insufficient_quota", "insufficient quota", "quota exceeded",
    "billing", "payment required", "out of credits",
    "rate limit", "rate_limit", "ratelimit", "too many requests",
    "content policy", "policy violation", "moderation",
)
_HTTP_STATUS_RE = re.compile(r"\bhttp[ _=:]*(\d{3})\b|\bstatus(?:_code)?[ _=:]*(\d{3})\b")

# Deterministic TLS failures (bad proxy CA, expired/self-signed cert, hostname
# mismatch): retrying won't fix them, so they are not connectivity candidates.
_TLS_CERT_PATTERNS = (
    "certificate verify failed", "certificate_verify_failed",
    "unable to get local issuer certificate",
    "self-signed certificate", "self signed certificate",
    "certificate has expired", "hostname mismatch",
    "unable to verify the first certificate",
)

_DNS_MESSAGE_PATTERNS = (
    "name or service not known", "temporary failure in name resolution",
    "nodename nor servname", "getaddrinfo",
    "eai_again", "eai_noname", "eai_fail",
    "no such host", "name resolution", "dns",
)

_TIMEOUT_MESSAGE_PATTERNS = ("timed out", "timeout")

_TCP_MESSAGE_PATTERNS = (
    "connection refused", "econnrefused",
    "connection reset", "econnreset",
    "connection aborted", "econnaborted",
    "broken pipe", "epipe",
    "network is unreachable", "network unreachable", "enetunreach",
    "no route to host", "ehostunreach", "ehostdown", "enetdown",
    "connection error",
)
_TCP_ERRNOS = frozenset({
    errno.ECONNREFUSED, errno.ECONNRESET, errno.EPIPE,
    errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
    errno.EHOSTDOWN, errno.ECONNABORTED, errno.ENETRESET,
})
_TCP_EXC_TYPES = (
    ConnectionRefusedError, ConnectionResetError,
    ConnectionAbortedError, BrokenPipeError,
)


def _iter_chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception plus its ``__cause__``/``__context__`` chain (cycle-safe).

    SDK errors routinely wrap the real socket failure one or two levels deep;
    classifying only the outermost message would miss it.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _message_of(exc: BaseException) -> str:
    return " ".join(str(part) for part in _iter_chain(exc)).lower()


def _has_patterns(message: str, patterns: tuple[str, ...]) -> bool:
    return any(p in message for p in patterns)


def _carries_http_status(exc: BaseException) -> bool:
    """True when any link in the chain carries an HTTP status (attr or text)."""
    for part in _iter_chain(exc):
        for attr in ("status_code", "status"):
            if isinstance(getattr(part, attr, None), int):
                return True
        response = getattr(part, "response", None)
        if isinstance(getattr(response, "status_code", None), int):
            return True
    return _HTTP_STATUS_RE.search(_message_of(exc)) is not None


def classify_transport_failure(exc: BaseException) -> str:
    """Classify an exception from a failed model API call.

    Returns ``"dns"`` / ``"tcp"`` / ``"timeout"`` for local-link failures,
    ``"tls_cert"`` for deterministic certificate failures, ``"http"`` for
    anything that reached the server (status codes, auth, billing, policy), and
    ``"other"`` for the rest. Only the first three count as connectivity-loss
    candidates for the pause path.
    """
    if _carries_http_status(exc) or _has_patterns(_message_of(exc), _HTTP_MESSAGE_PATTERNS):
        return HTTP
    message = _message_of(exc)
    if _has_patterns(message, _TLS_CERT_PATTERNS):
        return TLS_CERT
    chain = list(_iter_chain(exc))
    if any(isinstance(part, socket.gaierror) for part in chain) \
            or _has_patterns(message, _DNS_MESSAGE_PATTERNS):
        return DNS
    if any(isinstance(part, TimeoutError) for part in chain) \
            or _has_patterns(message, _TIMEOUT_MESSAGE_PATTERNS):
        return TIMEOUT
    if any(isinstance(part, _TCP_EXC_TYPES) for part in chain) \
            or any(getattr(part, "errno", None) in _TCP_ERRNOS for part in chain) \
            or _has_patterns(message, _TCP_MESSAGE_PATTERNS):
        return TCP
    return OTHER


@dataclass(frozen=True)
class ProbeOutcome:
    provider_reachable: bool
    network_reachable: bool


_DEFAULT_NEUTRAL_PROBES = ("1.1.1.1:443", "8.8.8.8:53")
_PROVIDER_PROBE_PORT = 443
_MIN_TIMEOUT_S = 0.5
# Last-resort provider host when the agent has no usable base_url (documented in
# provider_host_for): the default unified-gateway host — reaching it is a sound
# stand-in for "the internet works".
_FALLBACK_PROVIDER_HOST = "openrouter.ai"


def _parse_host_port(entry: str, default_port: int) -> tuple[str, int] | None:
    """``(host, port)`` for a ``"host:port"`` entry, URL, or bare hostname."""
    raw = (entry or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:  # non-numeric / out-of-range port
        return None
    return (host, port if port else default_port)


def _tcp_reachable(host: str, port: int, timeout_s: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except Exception as exc:  # an unreachable host is an expected outcome
        logger.debug("connectivity probe %s:%d unreachable: %r", host, port, exc)
        return False


def probe_connectivity(
    provider_host: str,
    probe_hosts: list[str] | None = None,
    timeout_s: float = 3.0,
) -> ProbeOutcome:
    """Dial the provider host and neutral internet endpoints; never raises.

    ``provider_reachable`` dials ``provider_host:443`` (host portion only — a
    bare hostname or full URL both work). ``network_reachable`` dials each
    ``"host:port"`` entry in ``probe_hosts`` (default Cloudflare/Google) and is
    true when at least one answers. Interpretation: provider up → normal retry;
    provider down + network up → provider outage (existing fallback path); both
    down → local connectivity loss. Worst case blocks ~``timeout_s`` per host.
    """
    timeout = max(float(timeout_s or 0.0), _MIN_TIMEOUT_S)
    neutral = list(probe_hosts) if probe_hosts else list(_DEFAULT_NEUTRAL_PROBES)

    provider_target = _parse_host_port(provider_host, _PROVIDER_PROBE_PORT)
    provider_reachable = (
        _tcp_reachable(provider_target[0], _PROVIDER_PROBE_PORT, timeout)
        if provider_target is not None else False
    )

    network_reachable = False
    for entry in neutral:
        target = _parse_host_port(entry, _PROVIDER_PROBE_PORT)
        if target is None:
            logger.debug("skipping malformed probe entry %r", entry)
            continue
        if _tcp_reachable(target[0], target[1], timeout):
            network_reachable = True
            break
    return ProbeOutcome(
        provider_reachable=provider_reachable,
        network_reachable=network_reachable,
    )


def provider_host_for(agent: Any) -> str:
    """Hostname of the model API endpoint the agent is currently routed to.

    Reads ``agent.base_url`` (already provider-resolved by the time the turn
    loop runs). Falls back to ``openrouter.ai`` when it is missing or
    unparseable — documented last resort, not a guess at the user's provider.
    """
    base_url = str(getattr(agent, "base_url", "") or "").strip()
    host = _parse_host_port(base_url, _PROVIDER_PROBE_PORT)
    if host is not None:
        return host[0]
    logger.debug(
        "provider_host_for: no usable base_url %r; falling back to %s",
        base_url, _FALLBACK_PROVIDER_HOST,
    )
    return _FALLBACK_PROVIDER_HOST
