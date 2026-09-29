"""Unit tests for agent/connectivity_probe.py.

All network interaction is faked at ``socket.create_connection`` — these tests
never touch the real network.
"""

from __future__ import annotations

import errno
import socket
import ssl
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent import connectivity_probe
from agent.connectivity_probe import (
    ProbeOutcome,
    classify_transport_failure,
    probe_connectivity,
    provider_host_for,
)


# ── classify_transport_failure ────────────────────────────────────────────

def test_classify_dns_gaierror() -> None:
    exc = socket.gaierror(-2, "Name or service not known")
    assert classify_transport_failure(exc) == "dns"


def test_classify_dns_message_only() -> None:
    assert classify_transport_failure(Exception("Temporary failure in name resolution")) == "dns"
    assert classify_transport_failure(Exception("getaddrinfo failed")) == "dns"


def test_classify_tcp_refused_and_reset() -> None:
    assert classify_transport_failure(ConnectionRefusedError(111, "Connection refused")) == "tcp"
    assert classify_transport_failure(ConnectionResetError(104, "Connection reset by peer")) == "tcp"
    assert classify_transport_failure(BrokenPipeError(32, "Broken pipe")) == "tcp"


def test_classify_tcp_errno() -> None:
    assert classify_transport_failure(OSError(errno.ENETUNREACH, "Network is unreachable")) == "tcp"
    assert classify_transport_failure(OSError(errno.EHOSTUNREACH, "No route to host")) == "tcp"


def test_classify_tcp_sdk_style_message() -> None:
    # httpx/openai wrap the errno in a plain message; no OSError subclass survives.
    assert classify_transport_failure(Exception("[Errno 111] Connection refused")) == "tcp"
    assert classify_transport_failure(Exception("Connection error.")) == "tcp"


def test_classify_timeout() -> None:
    assert classify_transport_failure(socket.timeout("timed out")) == "timeout"
    assert classify_transport_failure(TimeoutError("Connection timed out")) == "timeout"


def test_classify_tls_cert_not_transport() -> None:
    # ssl.SSLError is an OSError subclass — the cert check must win over errno matching.
    exc = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    assert classify_transport_failure(exc) == "tls_cert"
    assert classify_transport_failure(Exception("self-signed certificate in chain")) == "tls_cert"


def test_classify_http_status_attribute() -> None:
    for status in (400, 401, 403, 429, 500, 503):
        exc = SimpleNamespace(status_code=status)
        assert classify_transport_failure(exc) == "http", status


def test_classify_http_response_attribute() -> None:
    exc = SimpleNamespace(response=SimpleNamespace(status_code=500))
    assert classify_transport_failure(exc) == "http"


def test_classify_http_status_in_message() -> None:
    assert classify_transport_failure(Exception("HTTP 503 Service Unavailable")) == "http"
    assert classify_transport_failure(Exception("request failed with status_code=429")) == "http"


def test_classify_http_auth_billing_policy() -> None:
    assert classify_transport_failure(Exception("401 Unauthorized: invalid api key")) == "http"
    assert classify_transport_failure(Exception("insufficient_quota: you exceeded your current quota")) == "http"
    assert classify_transport_failure(Exception("Rate limit exceeded, retry later")) == "http"
    assert classify_transport_failure(Exception("content policy violation")) == "http"


def test_classify_other() -> None:
    assert classify_transport_failure(ValueError("boom")) == "other"
    assert classify_transport_failure(Exception("something unexpected")) == "other"


def test_classify_walks_cause_chain() -> None:
    outer = RuntimeError("request failed")
    outer.__cause__ = ConnectionRefusedError(111, "Connection refused")
    assert classify_transport_failure(outer) == "tcp"


# ── probe_connectivity ────────────────────────────────────────────────────

class _FakeSocket:
    """Context manager standing in for a connected socket."""

    def __enter__(self) -> "_FakeSocket":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _install_fake_create_connection(monkeypatch: pytest.MonkeyPatch, up: set[tuple[str, int]]) -> list[tuple[str, int, float]]:
    """Fake ``socket.create_connection``: ``up`` hosts succeed, the rest refuse.

    Returns the list of ``(host, port, timeout)`` dial attempts, in order.
    """
    calls: list[tuple[str, int, float]] = []

    def fake(address: tuple[str, int], timeout: float | None = None, **kwargs: Any) -> _FakeSocket:
        host, port = address
        calls.append((host, port, timeout if timeout is not None else -1.0))
        if (host, port) in up:
            return _FakeSocket()
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(socket, "create_connection", fake)
    return calls


@pytest.mark.parametrize(
    ("provider_up", "network_up", "expected"),
    [
        (True, True, ProbeOutcome(True, True)),     # everything fine
        (True, False, ProbeOutcome(True, False)),   # neutral hosts blocked, provider fine
        (False, True, ProbeOutcome(False, True)),   # provider outage
        (False, False, ProbeOutcome(False, False)), # local connectivity loss
    ],
)
def test_probe_reachability_matrix(
    monkeypatch: pytest.MonkeyPatch,
    provider_up: bool,
    network_up: bool,
    expected: ProbeOutcome,
) -> None:
    up = {("api.example.com", 443)} if provider_up else set()
    if network_up:
        up.add(("1.1.1.1", 443))
    calls = _install_fake_create_connection(monkeypatch, up)

    outcome = probe_connectivity("api.example.com", timeout_s=3.0)

    assert outcome == expected
    dialed = {(host, port) for host, port, _ in calls}
    assert ("api.example.com", 443) in dialed  # provider always probed on 443
    assert all(timeout == 3.0 for _, _, timeout in calls)


def test_probe_uses_default_neutral_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_create_connection(monkeypatch, set())

    outcome = probe_connectivity("https://api.example.com/v1", probe_hosts=None)

    assert outcome == ProbeOutcome(False, False)
    dialed = {(host, port) for host, port, _ in calls}
    assert ("api.example.com", 443) in dialed
    assert ("1.1.1.1", 443) in dialed
    assert ("8.8.8.8", 53) in dialed


def test_probe_stops_after_first_neutral_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_create_connection(
        monkeypatch, {("api.example.com", 443), ("1.1.1.1", 443), ("8.8.8.8", 53)})

    probe_connectivity("api.example.com", probe_hosts=["1.1.1.1:443", "8.8.8.8:53"])

    neutral_calls = [(h, p) for h, p, _ in calls if h in ("1.1.1.1", "8.8.8.8")]
    assert neutral_calls == [("1.1.1.1", 443)]


def test_probe_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def weird(address: tuple[str, int], timeout: float | None = None, **kwargs: Any) -> None:
        raise RuntimeError("something bizarre")

    monkeypatch.setattr(socket, "create_connection", weird)
    assert probe_connectivity("api.example.com", probe_hosts=["1.2.3.4:9999"]) == ProbeOutcome(False, False)


def test_probe_malformed_entries_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_create_connection(monkeypatch, set())
    outcome = probe_connectivity("", probe_hosts=["", "not a port:abc", "10.0.0.1:notaport"])
    assert outcome == ProbeOutcome(False, False)
    assert calls == []


def test_probe_accepts_url_provider_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_create_connection(monkeypatch, {("api.deepseek.com", 443)})
    outcome = probe_connectivity("https://api.deepseek.com/v1/chat")
    assert outcome.provider_reachable is True
    assert ("api.deepseek.com", 443) in {(h, p) for h, p, _ in calls}


# ── provider_host_for ─────────────────────────────────────────────────────

def test_provider_host_for_from_base_url() -> None:
    agent = SimpleNamespace(base_url="https://api.deepseek.com/v1")
    assert provider_host_for(agent) == "api.deepseek.com"


def test_provider_host_for_bare_hostname() -> None:
    assert provider_host_for(SimpleNamespace(base_url="llm.internal:8443")) == "llm.internal"


def test_provider_host_for_falls_back_without_base_url() -> None:
    assert provider_host_for(SimpleNamespace(base_url="")) == "openrouter.ai"
    assert provider_host_for(SimpleNamespace()) == "openrouter.ai"
    assert provider_host_for(None) == "openrouter.ai"


def test_probe_module_import_has_no_heavy_deps() -> None:
    # The module must stay stdlib-only so the turn loop can import it cheaply.
    assert connectivity_probe.socket is socket
    assert not hasattr(connectivity_probe, "requests")
