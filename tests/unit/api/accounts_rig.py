"""Shared rig for the accounts and telemetry API tests: a real engine, server and accounts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.unit.auth.conftest import CHEAP, GOOD, SETUP_TOKEN
from vigil.api.app import create_app
from vigil.api.server import ApiServer
from vigil.api.session import REQUEST_HEADER, SESSION_SCOPE
from vigil.auth.audit import AuditLog
from vigil.auth.passwords import PasswordHasherService
from vigil.auth.roles import Permission
from vigil.auth.service import AuthService
from vigil.auth.store import AccountStore
from vigil.config.loader import LoadedConfig
from vigil.core.clock import SystemClock
from vigil.observability.logging import get_logger
from vigil.pipeline.app import Application
from vigil.pipeline.streaming import StreamHub

MakeConfig = Callable[..., LoadedConfig]
CSRF = {REQUEST_HEADER: "1"}
TOKEN = "b" * 40
VALID_SCOPES = {p.value for p in Permission} | {SESSION_SCOPE}
MUTATING = {"post", "put", "patch", "delete"}


@dataclass
class Rig:
    server: ApiServer
    service: AuthService
    base: str
    app: Application

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base, timeout=15)

    def origin(self) -> dict[str, str]:
        return {**CSRF, "Origin": self.base}


@pytest.fixture
def rig(make_config: MakeConfig, tmp_path: Path) -> Iterator[Rig]:
    cfg = make_config(
        {"api.auth.enabled": True, "api.auth.token": TOKEN, "api.accounts.enabled": True}
    )
    hub = StreamHub()
    app = Application(cfg.settings, cfg.paths, stream_hub=hub)
    app.start()
    store = AccountStore(tmp_path / "auth.sqlite3")
    clock = SystemClock()
    service = AuthService(
        store,
        AuditLog(store, clock),
        PasswordHasherService(CHEAP),
        clock,
        setup_token=SETUP_TOKEN,
    )
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>console</title>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("not for the web", encoding="utf-8")
    server = ApiServer(
        create_app(app, hub, accounts=service, ui_dir=ui),
        host="127.0.0.1",
        port=0,
        logger=get_logger("t"),
    )
    server.start()
    try:
        yield Rig(server, service, server.url, app)
    finally:
        server.stop()
        app.stop()
        store.close()


@contextmanager
def signed_in(rig: Rig, name: str, password: str = GOOD) -> Iterator[httpx.Client]:
    with rig.client() as c:
        r = c.post(
            "/api/v1/auth/login",
            json={"username": name, "password": password},
            headers=rig.origin(),
        )
        assert r.status_code == 200, r.text
        c.headers.update(rig.origin())
        yield c


def set_up(rig: Rig) -> None:
    with rig.client() as c:
        r = c.post(
            "/api/v1/auth/setup",
            json={"token": SETUP_TOKEN, "username": "alice", "password": GOOD},
            headers=rig.origin(),
        )
        assert r.status_code == 201, r.text


def make_user(rig: Rig, name: str, role: str) -> None:
    """Create a user through the API and clear the forced password change, as a person would."""
    with signed_in(rig, "alice") as admin:
        r = admin.post(
            "/api/v1/users", json={"username": name, "password": GOOD + "!", "role": role}
        )
        assert r.status_code == 201, r.text
    with signed_in(rig, name, GOOD + "!") as c:
        r = c.post("/api/v1/auth/password", json={"current": GOOD + "!", "new": GOOD + "?"})
        assert r.status_code == 204, r.text
