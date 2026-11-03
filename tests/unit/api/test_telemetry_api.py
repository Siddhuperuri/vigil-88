"""The live channel over a real WebSocket: who may open it, what it pushes, and when it closes."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import ClientConnection, connect

from tests.unit.api.accounts_rig import Rig, make_user, set_up, signed_in
from tests.unit.api.test_incident_api import _incident, _seed, _ulids
from vigil.api.routers.telemetry import CLOSE_LOCKED, CLOSE_SIGNED_OUT

pytestmark = pytest.mark.timeout(60)


def ws_url(rig: Rig) -> str:
    return rig.base.replace("http://", "ws://") + "/api/v1/telemetry"


@contextmanager
def socket(rig: Rig, cookie: str | None, origin: str | None = "same") -> Iterator[ClientConnection]:
    headers: dict[str, str] = {}
    if cookie is not None:
        headers["Cookie"] = f"vigil_session={cookie}"
    kwargs: dict[str, object] = {"additional_headers": headers, "open_timeout": 10}
    if origin == "same":
        kwargs["origin"] = rig.base
    elif origin is not None:
        kwargs["origin"] = origin
    with connect(ws_url(rig), **kwargs) as ws:  # type: ignore[arg-type]
        yield ws


def messages(ws: ClientConnection, until: float = 6.0) -> Iterator[dict[str, object]]:
    end = time.monotonic() + until
    while time.monotonic() < end:
        try:
            yield json.loads(ws.recv(timeout=1.0))
        except TimeoutError:
            continue


def refused(rig: Rig, cookie: str | None, origin: str | None = "same") -> int:
    with pytest.raises(InvalidStatus) as info, socket(rig, cookie, origin):
        pass
    return info.value.response.status_code


def test_an_anonymous_upgrade_is_refused_before_it_is_accepted(rig: Rig) -> None:
    set_up(rig)
    assert refused(rig, None) == 401


def test_the_upgrade_needs_a_matching_origin_so_another_site_cannot_ride_the_cookie(
    rig: Rig,
) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as c:
        cookie = c.cookies.get("vigil_session")
    assert refused(rig, cookie, origin="http://evil.example") == 403
    assert refused(rig, cookie, origin=None) == 403


def test_a_signed_in_user_first_receives_a_snapshot_of_every_camera(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as c:
        cookie = c.cookies.get("vigil_session")
    with socket(rig, cookie) as ws:
        first = json.loads(ws.recv(timeout=10))
    assert first["kind"] == "snapshot" and first["state"] == "running"
    assert first["health"]["level"] in {"ok", "degraded", "failed"}
    assert isinstance(first["cameras"], list)


def test_a_viewer_may_watch(rig: Rig) -> None:
    set_up(rig)
    make_user(rig, "vera", "viewer")
    with signed_in(rig, "vera", "correct horse battery?") as c:
        cookie = c.cookies.get("vigil_session")
    with socket(rig, cookie) as ws:
        assert json.loads(ws.recv(timeout=10))["kind"] == "snapshot"


def test_an_unchanged_engine_still_sends_a_heartbeat_so_a_frozen_link_is_visible(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as c:
        cookie = c.cookies.get("vigil_session")
    with socket(rig, cookie) as ws:
        snapshots = [m for m in messages(ws, until=6.0) if m["kind"] == "snapshot"]
    assert len(snapshots) >= 2  # the first, then at least one heartbeat within a few seconds


def test_a_new_incident_is_pushed_once_and_a_state_change_is_pushed_as_an_update(
    rig: Rig,
) -> None:
    set_up(rig)
    response = rig.app.response
    assert response is not None
    with signed_in(rig, "alice") as c:
        cookie = c.cookies.get("vigil_session")
        with socket(rig, cookie) as ws:
            json.loads(ws.recv(timeout=10))  # baseline: nothing yet
            ulids = _ulids()
            made = _seed(response, ulids, _incident(ulids))
            seen = [m for m in messages(ws, until=5.0) if m["kind"] == "incident"]
            assert [m["change"] for m in seen] == ["new"]
            assert seen[0]["incident"]["incident_id"] == made.incident_id
            r = c.post(f"/api/v1/incidents/{made.incident_id}/acknowledge", json={})
            assert r.status_code == 200, r.text
            later = [m for m in messages(ws, until=5.0) if m["kind"] == "incident"]
            assert [m["change"] for m in later] == ["updated"]
            assert later[0]["incident"]["status"] == "active"


def drain(ws: ClientConnection, seconds: float = 8.0) -> None:
    """Read until the server closes the socket, which raises ConnectionClosed."""
    for _ in messages(ws, until=seconds):
        pass
    ws.recv(timeout=2)


def test_locking_the_console_closes_the_socket_and_logging_out_does_too(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as c:
        cookie = c.cookies.get("vigil_session")
        with socket(rig, cookie) as ws:
            ws.recv(timeout=10)
            assert c.post("/api/v1/auth/lock").status_code == 204
            with pytest.raises(ConnectionClosed) as closed:
                drain(ws)
            assert closed.value.rcvd is not None and closed.value.rcvd.code == CLOSE_LOCKED
        c.post("/api/v1/auth/unlock", json={"password": "correct horse battery"})
        with socket(rig, cookie) as ws2:
            ws2.recv(timeout=10)
            c.post("/api/v1/auth/logout")
            with pytest.raises(ConnectionClosed) as out:
                drain(ws2)
            assert out.value.rcvd is not None and out.value.rcvd.code == CLOSE_SIGNED_OUT
