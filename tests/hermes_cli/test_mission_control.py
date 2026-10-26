"""Tests for GET /api/sessions/mission-control."""
import time
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lease(session_id, surface="cli", started_at=None, updated_at=None, lease_id=None):
    now = time.time()
    return {
        "lease_id": lease_id or "lease-" + session_id,
        "session_id": session_id,
        "surface": surface,
        "pid": 12345,
        "process_start_time": now - 60,
        "started_at": started_at or now - 30,
        "updated_at": updated_at or now - 5,
    }


def _make_db_row(session_id, title="Test session", source="cli", cwd="/tmp",
                 started_at=None, last_active=None):
    now = time.time()
    return {
        "id": session_id,
        "title": title,
        "source": source,
        "cwd": cwd,
        "started_at": started_at or now - 30,
        "last_active": last_active or now - 5,
        "ended_at": None,
    }


def _mock_db(rows_by_sid):
    """Return a mock SessionDB whose get_session returns from rows_by_sid."""
    db = MagicMock()
    db.get_session.side_effect = lambda sid: rows_by_sid.get(sid)
    db.close = MagicMock()
    return db


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch, _isolate_hermes_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def _make_presence(session_id, surface="cli", busy=False, title="", cwd="",
                   pid=12345, updated_at=None):
    now = time.time()
    return {
        "session_id": session_id,
        "pid": pid,
        "process_start_time": now - 60,
        "surface": surface,
        "busy": busy,
        "inbound": "accept",
        "title": title,
        "cwd": cwd,
        "updated_at": updated_at if updated_at is not None else now - 5,
    }


def _mission(client, leases, db_rows=None, relay_has_turn=False, profiles=None,
             presences=None):
    """Drive GET /api/sessions/mission-control with controlled dependencies.

    ``presences`` is an optional dict {session_id: presence_record}.  When
    provided, ``peer_inbox.live_presence_ids`` and ``read_presence`` are stubbed
    accordingly.  When omitted, peer_inbox returns no presences (avoids
    accidentally picking up live machine state in CI).
    """
    db_rows = db_rows or {}
    profiles = profiles or [{"name": None, "path": None}]
    presences = presences or {}

    coord = MagicMock()
    coord.has_active_turn = MagicMock(return_value=relay_has_turn)

    mock_pi = MagicMock()
    mock_pi.live_presence_ids.return_value = list(presences.keys())
    mock_pi.read_presence.side_effect = lambda sid: presences.get(sid)

    with patch("hermes_cli.active_sessions.active_session_registry_snapshot",
               return_value=leases), \
         patch("hermes_cli.web_server_cron._cron_profile_dicts",
               return_value=profiles), \
         patch("hermes_cli.web_routers.sessions._open_session_db_for_profile",
               return_value=_mock_db(db_rows)), \
         patch("agent.relay_runtime.SESSION_COORDINATOR", coord, create=True), \
         patch("tools.peer_inbox", mock_pi, create=True):
        resp = client.get("/api/sessions/mission-control")

    return resp


# ---------------------------------------------------------------------------
# Tests: empty / no leases
# ---------------------------------------------------------------------------

class TestMissionControlEmpty:

    def test_no_leases_returns_empty(self, client):
        resp = _mission(client, leases=[])
        assert resp.status_code == 200
        assert resp.json()["sessions"] == []

    def test_registry_exception_returns_empty(self, client):
        with patch("hermes_cli.active_sessions.active_session_registry_snapshot",
                   side_effect=OSError("test failure")):
            resp = client.get("/api/sessions/mission-control")
        assert resp.status_code == 200
        assert resp.json()["sessions"] == []


# ---------------------------------------------------------------------------
# Tests: field presence
# ---------------------------------------------------------------------------

class TestMissionControlFields:

    def test_required_fields_present(self, client):
        sid = "sess-fields"
        resp = _mission(
            client,
            leases=[_make_lease(sid, surface="cli")],
            db_rows={sid: _make_db_row(sid, title="My session", cwd="/home/user")},
        )
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        row = sessions[0]
        for field in ("id", "title", "source", "profile", "cwd", "status",
                      "idle_since", "last_active", "started_at"):
            assert field in row, "Missing field: {}".format(field)
        assert row["id"] == sid
        assert row["title"] == "My session"
        assert row["cwd"] == "/home/user"
        assert row["status"] in ("working", "awaiting_input", "idle")

    def test_partial_row_when_no_db_match(self, client):
        """Session in lease but not in any DB → partial row, no crash."""
        sid = "unknown-sid"
        resp = _mission(
            client,
            leases=[_make_lease(sid, surface="cli")],
            db_rows={},  # no DB row
        )
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        row = sessions[0]
        assert row["id"] == sid
        assert row["title"] == ""
        assert row["status"] in ("working", "awaiting_input", "idle")


# ---------------------------------------------------------------------------
# Tests: status classification
# ---------------------------------------------------------------------------

class TestMissionControlStatus:

    def test_working_when_relay_active_turn(self, client):
        sid = "sess-working"
        resp = _mission(
            client,
            leases=[_make_lease(sid, surface="cli")],
            db_rows={sid: _make_db_row(sid)},
            relay_has_turn=True,
        )
        row = resp.json()["sessions"][0]
        assert row["status"] == "working"
        assert row["idle_since"] is None

    def test_awaiting_input_cli_no_active_turn(self, client):
        sid = "sess-awaiting"
        resp = _mission(
            client,
            leases=[_make_lease(sid, surface="cli")],
            db_rows={sid: _make_db_row(sid)},
            relay_has_turn=False,
        )
        row = resp.json()["sessions"][0]
        assert row["status"] == "awaiting_input"
        assert row["idle_since"] is not None

    def test_awaiting_input_tui_surface(self, client):
        sid = "sess-tui"
        resp = _mission(
            client,
            leases=[_make_lease(sid, surface="tui")],
            db_rows={sid: _make_db_row(sid)},
            relay_has_turn=False,
        )
        assert resp.json()["sessions"][0]["status"] == "awaiting_input"

    def test_awaiting_input_desktop_surface(self, client):
        sid = "sess-desktop"
        resp = _mission(
            client,
            leases=[_make_lease(sid, surface="desktop")],
            db_rows={sid: _make_db_row(sid)},
            relay_has_turn=False,
        )
        assert resp.json()["sessions"][0]["status"] == "awaiting_input"

    def test_idle_for_gateway_surface(self, client):
        sid = "sess-gateway"
        resp = _mission(
            client,
            leases=[_make_lease(sid, surface="gateway")],
            db_rows={sid: _make_db_row(sid)},
            relay_has_turn=False,
        )
        assert resp.json()["sessions"][0]["status"] == "idle"

    def test_idle_for_unknown_surface(self, client):
        sid = "sess-unknown"
        resp = _mission(
            client,
            leases=[_make_lease(sid, surface="tool")],
            db_rows={sid: _make_db_row(sid)},
            relay_has_turn=False,
        )
        assert resp.json()["sessions"][0]["status"] == "idle"


# ---------------------------------------------------------------------------
# Tests: sort order
# ---------------------------------------------------------------------------

class TestMissionControlSortOrder:

    def test_working_first_then_awaiting_then_idle(self, client):
        now = time.time()
        idle_sid = "sess-idle"
        working_sid = "sess-work"
        awaiting_sid = "sess-await"

        leases = [
            _make_lease(idle_sid, surface="gateway"),
            _make_lease(working_sid, surface="cli"),
            _make_lease(awaiting_sid, surface="cli"),
        ]
        db_rows = {
            idle_sid: _make_db_row(idle_sid, last_active=now - 10),
            working_sid: _make_db_row(working_sid, last_active=now - 5),
            awaiting_sid: _make_db_row(awaiting_sid, last_active=now - 15),
        }

        db = _mock_db(db_rows)
        # Only working_sid reports an active turn.
        coord = MagicMock()
        coord.has_active_turn = MagicMock(
            side_effect=lambda profile_key, session_id: session_id == working_sid
        )

        with patch("hermes_cli.active_sessions.active_session_registry_snapshot",
                   return_value=leases), \
             patch("hermes_cli.web_server_cron._cron_profile_dicts",
                   return_value=[{"name": None, "path": None}]), \
             patch("hermes_cli.web_routers.sessions._open_session_db_for_profile",
                   return_value=db), \
             patch("agent.relay_runtime.SESSION_COORDINATOR", coord, create=True):
            resp = client.get("/api/sessions/mission-control")

        assert resp.status_code == 200
        rows = resp.json()["sessions"]
        assert len(rows) == 3
        assert rows[0]["status"] == "working"
        assert rows[0]["id"] == working_sid
        statuses_rest = {r["status"] for r in rows[1:]}
        assert statuses_rest == {"awaiting_input", "idle"}

    def test_within_same_status_sorted_by_last_active_desc(self, client):
        now = time.time()
        sid_recent = "sess-recent"
        sid_old = "sess-old"

        leases = [
            _make_lease(sid_old, surface="cli"),
            _make_lease(sid_recent, surface="cli"),
        ]
        db_rows = {
            sid_old: _make_db_row(sid_old, last_active=now - 100),
            sid_recent: _make_db_row(sid_recent, last_active=now - 5),
        }

        resp = _mission(client, leases=leases, db_rows=db_rows, relay_has_turn=False)
        rows = resp.json()["sessions"]
        assert len(rows) == 2
        # More recent last_active comes first.
        assert rows[0]["id"] == sid_recent
        assert rows[1]["id"] == sid_old


# ---------------------------------------------------------------------------
# Tests: multi-profile
# ---------------------------------------------------------------------------

class TestMissionControlMultiProfile:

    def test_sessions_resolved_across_profiles(self, client):
        sid_a = "sess-profile-a"
        sid_b = "sess-profile-b"

        leases = [
            _make_lease(sid_a, surface="cli"),
            _make_lease(sid_b, surface="cli"),
        ]
        profiles = [
            {"name": "alpha", "path": "/fake/.hermes/profiles/alpha"},
            {"name": "beta", "path": "/fake/.hermes/profiles/beta"},
        ]

        # alpha DB has sid_a; beta DB has sid_b.
        def _open_db_for_profile(profile_name, *, read_only):
            if profile_name == "alpha":
                return _mock_db({sid_a: _make_db_row(sid_a, title="Alpha session")})
            if profile_name == "beta":
                return _mock_db({sid_b: _make_db_row(sid_b, title="Beta session")})
            return _mock_db({})

        coord = MagicMock()
        coord.has_active_turn.return_value = False

        with patch("hermes_cli.active_sessions.active_session_registry_snapshot",
                   return_value=leases), \
             patch("hermes_cli.web_server_cron._cron_profile_dicts",
                   return_value=profiles), \
             patch("hermes_cli.web_routers.sessions._open_session_db_for_profile",
                   side_effect=_open_db_for_profile), \
             patch("agent.relay_runtime.SESSION_COORDINATOR", coord, create=True):
            resp = client.get("/api/sessions/mission-control")

        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 2
        by_id = {r["id"]: r for r in sessions}
        assert by_id[sid_a]["title"] == "Alpha session"
        assert by_id[sid_a]["profile"] == "alpha"
        assert by_id[sid_b]["title"] == "Beta session"
        assert by_id[sid_b]["profile"] == "beta"


# ---------------------------------------------------------------------------
# Tests: peer-inbox presence merge
# ---------------------------------------------------------------------------

class TestMissionControlPresenceMerge:
    """Verify that peer-inbox presence sessions are merged into the response."""

    def test_presence_only_session_appears(self, client):
        """A session with only a presence record (no lease) should appear."""
        sid = "pres-only-1"
        pres = _make_presence(sid, surface="cli")
        resp = _mission(client, leases=[], presences={sid: pres})
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["id"] == sid

    def test_presence_only_has_required_fields(self, client):
        sid = "pres-fields-1"
        pres = _make_presence(sid, surface="tui", title="My TUI", cwd="/home/u")
        resp = _mission(client, leases=[], presences={sid: pres})
        row = resp.json()["sessions"][0]
        for field in ("id", "title", "source", "profile", "cwd", "status",
                      "idle_since", "last_active", "started_at"):
            assert field in row, f"Missing field: {field}"
        assert row["cwd"] == "/home/u"
        assert row["title"] == "My TUI"

    def test_13_presence_sessions_all_appear(self, client):
        """Simulate 13 live terminal sessions visible only via peer_inbox."""
        presences = {
            f"term-{i}": _make_presence(f"term-{i}", surface="cli")
            for i in range(13)
        }
        resp = _mission(client, leases=[], presences=presences)
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 13
        ids = {s["id"] for s in sessions}
        assert ids == set(presences.keys())

    def test_lease_plus_presence_deduped(self, client):
        """Same session_id in both lease registry and presence → one row only."""
        sid = "dup-sid"
        lease = _make_lease(sid, surface="cli")
        pres = _make_presence(sid, surface="cli", title="from-presence")
        db_rows = {sid: _make_db_row(sid, title="from-db")}
        resp = _mission(client, leases=[lease], presences={sid: pres}, db_rows=db_rows)
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["id"] == sid
        # DB title wins over presence title when DB row exists
        assert sessions[0]["title"] == "from-db"

    def test_dedup_many_presence_with_some_leases(self, client):
        """13 presences + 3 of those also have leases → still 13 rows."""
        presences = {f"s{i}": _make_presence(f"s{i}", surface="cli") for i in range(13)}
        # 3 of the presence sessions also have lease entries
        leases = [_make_lease(f"s{i}") for i in range(3)]
        resp = _mission(client, leases=leases, presences=presences)
        sessions = resp.json()["sessions"]
        assert len(sessions) == 13

    def test_presence_title_cwd_used_when_no_db_row(self, client):
        """Presence title/cwd fill in when there is no DB row."""
        sid = "pres-title"
        pres = _make_presence(sid, surface="cli", title="Presence Title", cwd="/pres/cwd")
        resp = _mission(client, leases=[], presences={sid: pres}, db_rows={})
        row = resp.json()["sessions"][0]
        assert row["title"] == "Presence Title"
        assert row["cwd"] == "/pres/cwd"

    def test_presence_title_not_used_when_db_row_exists(self, client):
        """DB row title/cwd takes precedence over presence fields."""
        sid = "db-wins"
        pres = _make_presence(sid, surface="cli", title="Presence Title", cwd="/pres")
        db_rows = {sid: _make_db_row(sid, title="DB Title", cwd="/db")}
        resp = _mission(client, leases=[], presences={sid: pres}, db_rows=db_rows)
        row = resp.json()["sessions"][0]
        assert row["title"] == "DB Title"
        assert row["cwd"] == "/db"


# ---------------------------------------------------------------------------
# Tests: presence.busy → working
# ---------------------------------------------------------------------------

class TestMissionControlPresenceBusy:
    """presence.busy=True should set status=working without relay."""

    def test_presence_busy_sets_working(self, client):
        sid = "busy-1"
        pres = _make_presence(sid, surface="cli", busy=True)
        resp = _mission(client, leases=[], presences={sid: pres}, relay_has_turn=False)
        row = resp.json()["sessions"][0]
        assert row["status"] == "working"
        assert row["idle_since"] is None

    def test_presence_not_busy_awaiting_input_for_cli(self, client):
        sid = "idle-cli"
        pres = _make_presence(sid, surface="cli", busy=False)
        resp = _mission(client, leases=[], presences={sid: pres}, relay_has_turn=False)
        row = resp.json()["sessions"][0]
        assert row["status"] == "awaiting_input"

    def test_presence_not_busy_idle_for_gateway(self, client):
        sid = "idle-gw"
        pres = _make_presence(sid, surface="gateway", busy=False)
        resp = _mission(client, leases=[], presences={sid: pres})
        row = resp.json()["sessions"][0]
        assert row["status"] == "idle"

    def test_relay_working_wins_over_not_busy(self, client):
        """relay says active turn → working even if presence.busy=False."""
        sid = "relay-work"
        lease = _make_lease(sid, surface="cli")
        pres = _make_presence(sid, surface="cli", busy=False)
        resp = _mission(client, leases=[lease], presences={sid: pres}, relay_has_turn=True)
        row = resp.json()["sessions"][0]
        assert row["status"] == "working"

    def test_busy_true_wins_even_without_lease(self, client):
        """Presence-only entry with busy=True → working, no lease needed."""
        sid = "busyonly"
        pres = _make_presence(sid, surface="tui", busy=True)
        resp = _mission(client, leases=[], presences={sid: pres})
        row = resp.json()["sessions"][0]
        assert row["status"] == "working"

    def test_working_idle_since_is_none(self, client):
        sid = "busy-idle-since"
        pres = _make_presence(sid, surface="cli", busy=True)
        resp = _mission(client, leases=[], presences={sid: pres})
        row = resp.json()["sessions"][0]
        assert row["status"] == "working"
        assert row["idle_since"] is None


# ---------------------------------------------------------------------------
# Tests: idle_since uses real activity, not heartbeat
# ---------------------------------------------------------------------------

class TestMissionControlIdleSince:
    """idle_since should reflect db.last_active or lease.started_at; never
    presence.updated_at (heartbeat noise)."""

    def test_idle_since_uses_db_last_active(self, client):
        now = time.time()
        sid = "idle-since-db"
        real_last = now - 900  # 15 min ago
        heartbeat_ts = now - 3  # just now (heartbeat)
        pres = _make_presence(sid, surface="cli", busy=False, updated_at=heartbeat_ts)
        db_rows = {sid: _make_db_row(sid, last_active=real_last)}
        resp = _mission(client, leases=[], presences={sid: pres}, db_rows=db_rows)
        row = resp.json()["sessions"][0]
        assert row["status"] == "awaiting_input"
        # idle_since should be real_last, NOT heartbeat_ts
        assert abs(row["idle_since"] - real_last) < 1.0, (
            f"idle_since={row['idle_since']} expected≈{real_last} (db.last_active), "
            f"NOT heartbeat {heartbeat_ts}"
        )

    def test_idle_since_accepts_raw_last_activity_at(self, client):
        now = time.time()
        sid = "idle-since-raw-column"
        real_last = now - 600
        pres = _make_presence(sid, surface="tui", busy=False, updated_at=now - 1)
        row_data = _make_db_row(sid)
        row_data.pop("last_active", None)
        row_data["last_activity_at"] = real_last
        resp = _mission(client, leases=[], presences={sid: pres}, db_rows={sid: row_data})
        row = resp.json()["sessions"][0]
        assert abs(row["idle_since"] - real_last) < 1.0

    def test_idle_since_falls_back_to_lease_started_at_when_no_db(self, client):
        now = time.time()
        sid = "idle-since-lease"
        started = now - 300
        heartbeat_ts = now - 2  # very recent heartbeat
        pres = _make_presence(sid, surface="cli", busy=False, updated_at=heartbeat_ts)
        lease = _make_lease(sid, surface="cli", started_at=started)
        resp = _mission(client, leases=[lease], presences={sid: pres}, db_rows={})
        row = resp.json()["sessions"][0]
        # No DB row: last_active falls back to lease.started_at, not heartbeat
        assert abs(row["idle_since"] - started) < 1.0, (
            f"idle_since={row['idle_since']} expected≈{started} (lease.started_at), "
            f"NOT heartbeat {heartbeat_ts}"
        )

    def test_idle_since_none_when_working(self, client):
        now = time.time()
        sid = "working-idle"
        db_rows = {sid: _make_db_row(sid, last_active=now - 60)}
        pres = _make_presence(sid, surface="cli", busy=True)
        resp = _mission(client, leases=[], presences={sid: pres}, db_rows=db_rows)
        row = resp.json()["sessions"][0]
        assert row["status"] == "working"
        assert row["idle_since"] is None


# ---------------------------------------------------------------------------
# Tests: empty / all-presence empty
# ---------------------------------------------------------------------------

class TestMissionControlPresenceEmpty:

    def test_no_leases_no_presence_returns_empty(self, client):
        resp = _mission(client, leases=[], presences={})
        assert resp.status_code == 200
        assert resp.json()["sessions"] == []

    def test_peer_inbox_unavailable_still_serves_leases(self, client):
        """If tools.peer_inbox raises on import, lease entries still returned."""
        sid = "lease-only"
        lease = _make_lease(sid, surface="tui")

        coord = MagicMock()
        coord.has_active_turn = MagicMock(return_value=False)

        import importlib
        import sys

        # Temporarily hide tools.peer_inbox
        saved = sys.modules.pop("tools.peer_inbox", None)
        try:
            with patch("hermes_cli.active_sessions.active_session_registry_snapshot",
                       return_value=[lease]), \
                 patch("hermes_cli.web_server_cron._cron_profile_dicts",
                       return_value=[{"name": None, "path": None}]), \
                 patch("hermes_cli.web_routers.sessions._open_session_db_for_profile",
                       return_value=_mock_db({})), \
                 patch("agent.relay_runtime.SESSION_COORDINATOR", coord, create=True):
                resp = client.get("/api/sessions/mission-control")
        finally:
            if saved is not None:
                sys.modules["tools.peer_inbox"] = saved

        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["id"] == sid
