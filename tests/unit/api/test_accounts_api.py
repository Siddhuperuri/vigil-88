"""Accounts over real HTTP (ADR 0016): sessions, roles, lock, origin discipline, and the test
that makes it impossible to ship a route without declaring who may call it."""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from tests.unit.api.accounts_rig import (
    CSRF,
    GOOD,
    MUTATING,
    SETUP_TOKEN,
    TOKEN,
    VALID_SCOPES,
    Rig,
    make_user,
    set_up,
    signed_in,
)
from vigil.api.session import PUBLIC_ROUTES, REQUEST_HEADER, SESSION_SCOPE

# ------------------------------------------------------------------ the permission test


def unprotected(openapi: dict[str, object]) -> list[str]:
    """Every operation with no single access requirement that is not on the public list."""
    problems: list[str] = []
    paths = openapi["paths"]
    assert isinstance(paths, dict)
    for path, ops in paths.items():
        for method, op in ops.items():
            if (method.upper(), path) in PUBLIC_ROUTES:
                continue
            scopes = [s for req in op.get("security", []) for s in next(iter(req.values()))]
            if len(scopes) != 1 or scopes[0] not in VALID_SCOPES:
                problems.append(f"{method.upper()} {path}: scopes={scopes}")
    return problems


def test_every_route_declares_exactly_one_access_requirement(rig: Rig) -> None:
    schema = httpx.get(f"{rig.base}/openapi.json").json()
    assert unprotected(schema) == []


def test_every_public_route_still_exists_so_the_list_cannot_go_stale(rig: Rig) -> None:
    schema = httpx.get(f"{rig.base}/openapi.json").json()
    for method, path in PUBLIC_ROUTES:
        assert method.lower() in schema["paths"][path], f"{method} {path} is listed but gone"


def test_the_check_itself_fails_on_a_route_with_no_requirement() -> None:
    """A guard that cannot fail is not a guard: an unprotected route must be reported."""
    app = FastAPI()

    @app.get("/api/v1/oops")
    def oops() -> dict[str, str]:
        return {}

    assert unprotected(app.openapi()) == ["GET /api/v1/oops: scopes=[]"]


def test_a_route_with_two_requirements_is_reported_too() -> None:
    schema = {"paths": {"/x": {"get": {"security": [{"vigilSession": ["view", "configure"]}]}}}}
    assert len(unprotected(schema)) == 1


# ------------------------------------------------------------------ setup and sign-in


def test_before_setup_only_status_and_setup_answer(rig: Rig) -> None:
    with rig.client() as c:
        status = c.get("/api/v1/auth/status").json()
        assert status["setup_required"] and not status["authenticated"]
        assert c.get("/api/v1/cameras").status_code == 401


def test_setup_needs_the_token_and_sets_a_hardened_cookie(rig: Rig) -> None:
    with rig.client() as c:
        bad = c.post(
            "/api/v1/auth/setup",
            json={"token": "wrong", "username": "alice", "password": GOOD},
            headers=rig.origin(),
        )
        assert bad.status_code == 403
        ok = c.post(
            "/api/v1/auth/setup",
            json={"token": SETUP_TOKEN, "username": "alice", "password": GOOD},
            headers=rig.origin(),
        )
        assert ok.status_code == 201 and len(ok.json()["recovery_codes"]) == 10
        cookie = ok.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
        assert c.get("/api/v1/auth/me").json()["user"]["role"] == "administrator"
        again = c.post(
            "/api/v1/auth/setup",
            json={"token": SETUP_TOKEN, "username": "mallory", "password": GOOD},
            headers=rig.origin(),
        )
        assert again.status_code == 403


def test_the_live_stream_authenticates_by_cookie_so_an_img_tag_can_show_it(rig: Rig) -> None:
    set_up(rig)
    with rig.client() as anon:
        assert anon.get("/api/v1/cameras/anything/snapshot.jpg").status_code == 401
    with signed_in(rig, "alice") as c:
        # Not 401: the request got past authentication and failed on the (nonexistent) camera.
        assert c.get("/api/v1/cameras/anything/snapshot.jpg").status_code == 404


def test_wrong_password_and_unknown_user_look_the_same_on_the_wire(rig: Rig) -> None:
    set_up(rig)
    with rig.client() as c:
        a = c.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "x" * 14},
            headers=rig.origin(),
        )
        b = c.post(
            "/api/v1/auth/login",
            json={"username": "nobody", "password": "x" * 14},
            headers=rig.origin(),
        )
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (401, a.json())


def test_five_failures_answer_429_with_retry_after(rig: Rig) -> None:
    set_up(rig)
    with rig.client() as c:
        for _ in range(5):
            c.post(
                "/api/v1/auth/login",
                json={"username": "alice", "password": "x" * 14},
                headers=rig.origin(),
            )
        locked = c.post(
            "/api/v1/auth/login", json={"username": "alice", "password": GOOD}, headers=rig.origin()
        )
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) >= 1


def test_logout_ends_the_session_on_the_server_and_a_replayed_cookie_is_dead(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as c:
        stolen = c.cookies.get("vigil_session")
        assert stolen
        assert c.post("/api/v1/auth/logout").status_code == 204
        assert c.get("/api/v1/cameras").status_code == 401
    with rig.client() as replay:
        replay.cookies.set("vigil_session", stolen)
        assert replay.get("/api/v1/cameras").status_code == 401


# ------------------------------------------------------------------ request discipline


def test_a_state_change_without_the_custom_header_is_refused(rig: Rig) -> None:
    with rig.client() as c:
        r = c.post("/api/v1/auth/login", json={"username": "a", "password": "b" * 14})
    assert r.status_code == 403 and REQUEST_HEADER in r.json()["detail"]


def test_a_cross_origin_state_change_is_refused_even_with_the_header(rig: Rig) -> None:
    with rig.client() as c:
        r = c.post(
            "/api/v1/auth/login",
            json={"username": "a", "password": "b" * 14},
            headers={**CSRF, "Origin": "http://evil.example"},
        )
    assert r.status_code == 403 and "cross-origin" in r.json()["detail"]


def test_a_foreign_host_header_is_refused_which_stops_dns_rebinding(rig: Rig) -> None:
    with rig.client() as c:
        r = c.get("/api/v1/auth/status", headers={"Host": "evil.example"})
    assert r.status_code == 400


def test_reads_need_no_csrf_header_because_they_change_nothing(rig: Rig) -> None:
    with rig.client() as c:
        assert c.get("/api/v1/auth/status").status_code == 200


# ------------------------------------------------------------------ roles over HTTP


def test_a_viewer_can_look_and_cannot_act(rig: Rig) -> None:
    set_up(rig)
    make_user(rig, "vera", "viewer")
    with signed_in(rig, "vera", GOOD + "?") as v:
        assert v.get("/api/v1/cameras").status_code == 200
        denied = v.post("/api/v1/mutes", json={"duration_s": 60, "reason": "test"})
        assert denied.status_code == 403 and "operator" in denied.json()["detail"]
        assert v.get("/api/v1/users").status_code == 403
        assert v.get("/api/v1/audit").status_code == 403


def test_an_operator_can_act_but_cannot_administer(rig: Rig) -> None:
    set_up(rig)
    make_user(rig, "oscar", "operator")
    with signed_in(rig, "oscar", GOOD + "?") as o:
        made = o.post("/api/v1/mutes", json={"duration_s": 60, "reason": "contractors"})
        assert made.status_code == 201
        assert o.delete(f"/api/v1/mutes/{made.json()['mute_id']}").status_code == 204
        assert o.get("/api/v1/users").status_code == 403
        assert o.get("/api/v1/analytics/export").status_code == 200


def test_every_mutating_route_refuses_a_viewer(rig: Rig) -> None:
    """Not a sample: walk the OpenAPI document and try every route that changes something."""
    set_up(rig)
    make_user(rig, "vera", "viewer")
    schema = httpx.get(f"{rig.base}/openapi.json").json()
    tried = 0
    with signed_in(rig, "vera", GOOD + "?") as v:
        for path, ops in schema["paths"].items():
            for method, op in ops.items():
                scopes = [s for req in op.get("security", []) for s in next(iter(req.values()))]
                if method not in MUTATING or scopes in ([], [SESSION_SCOPE]):
                    continue
                url = path.replace("{incident_id}", "x").replace("{mute_id}", "x")
                url = url.replace("{user_id}", "x").replace("{camera_id}", "x")
                r = v.request(method.upper(), url, json={})
                assert r.status_code == 403, f"{method.upper()} {path} -> {r.status_code}"
                tried += 1
    assert tried >= 7  # incident actions, mutes, users: the walk really covered them


def test_the_last_administrator_is_protected_over_http(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as a:
        me = a.get("/api/v1/auth/me").json()["user"]["user_id"]
        assert a.delete(f"/api/v1/users/{me}").status_code == 409
        assert a.patch(f"/api/v1/users/{me}", json={"role": "viewer"}).status_code == 409


# ------------------------------------------------------------------ lock and forced change


def test_a_locked_console_answers_423_but_still_unlocks(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as c:
        assert c.post("/api/v1/auth/lock").status_code == 204
        assert c.get("/api/v1/cameras").status_code == 423
        assert c.get("/api/v1/cameras/x/snapshot.jpg").status_code == 423  # streams are gated too
        assert c.get("/api/v1/auth/me").json()["locked"] is True
        assert (
            c.post("/api/v1/auth/unlock", json={"password": "wrong wrong wrong"}).status_code == 401
        )
        assert c.post("/api/v1/auth/unlock", json={"password": GOOD}).status_code == 204
        assert c.get("/api/v1/cameras").status_code == 200


def test_a_user_with_a_temporary_password_must_change_it_first(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as a:
        a.post(
            "/api/v1/users", json={"username": "newbie", "password": GOOD + "!", "role": "viewer"}
        )
    with signed_in(rig, "newbie", GOOD + "!") as n:
        blocked = n.get("/api/v1/cameras")
        assert blocked.status_code == 403 and "password" in blocked.json()["detail"].lower()
        assert n.get("/api/v1/auth/me").status_code == 200  # and can still reach the way out
        n.post("/api/v1/auth/password", json={"current": GOOD + "!", "new": GOOD + "?"})
        assert n.get("/api/v1/cameras").status_code == 200


# ------------------------------------------------------------------ the shared token still works


def test_the_shared_bearer_token_is_an_operator_not_an_administrator(rig: Rig) -> None:
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with rig.client() as c:
        assert c.get("/api/v1/cameras", headers=auth).status_code == 200
        assert c.get("/api/v1/users", headers=auth).status_code == 403
        assert c.get("/api/v1/cameras", headers={"Authorization": "Bearer nope"}).status_code == 401


# ------------------------------------------------------------------ audit


def test_actions_are_audited_with_the_user_and_the_log_verifies(rig: Rig) -> None:
    set_up(rig)
    make_user(rig, "oscar", "operator")
    with signed_in(rig, "oscar", GOOD + "?") as o:
        mute = o.post("/api/v1/mutes", json={"duration_s": 60, "reason": "contractors"}).json()
        o.delete(f"/api/v1/mutes/{mute['mute_id']}")
    with signed_in(rig, "alice") as a:
        entries = a.get("/api/v1/audit", params={"limit": 200}).json()
        by_action = {e["action"]: e for e in entries}
        assert by_action["mute.created"]["username"] == "oscar"
        assert by_action["mute.revoked"]["username"] == "oscar"
        assert "login.success" in by_action and "user.created" in by_action
        assert a.get("/api/v1/audit/verify").json()["ok"] is True
        csv_text = a.get("/api/v1/audit/export").text
        assert csv_text.splitlines()[0].startswith("seq,at_utc,user,action")
        assert a.get("/api/v1/audit/export", params={"format": "json"}).json()


def test_the_mute_records_who_made_it(rig: Rig) -> None:
    set_up(rig)
    make_user(rig, "oscar", "operator")
    with signed_in(rig, "oscar", GOOD + "?") as o:
        o.post("/api/v1/mutes", json={"duration_s": 60, "reason": "contractors"})
        shown = o.get("/api/v1/mutes").json()
    assert shown and shown[0]["actor"] == "oscar"


def test_audit_export_neutralises_spreadsheet_formulas(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as a:
        rig.service.audit.record("=cmd|' /c calc'!A0", target="=1+1")
        rows = a.get("/api/v1/audit/export").text
    assert "'=cmd" in rows and ",=cmd" not in rows


# ------------------------------------------------------------------ the built console


def test_the_built_console_is_served_on_the_api_origin_without_a_sign_in(rig: Rig) -> None:
    """It is static code, not data: every byte of data still passes the guard."""
    with rig.client() as c:
        page = c.get("/app/")
        assert page.status_code == 200 and "<title>console</title>" in page.text
        assert c.get("/api/v1/cameras").status_code == 401


def test_the_static_mount_cannot_be_walked_out_of(rig: Rig) -> None:
    with rig.client() as c:
        for path in ("/app/../secret.txt", "/app/%2e%2e/secret.txt", "/app/..%5csecret.txt"):
            r = c.get(path)
            assert "not for the web" not in r.text, path


def test_the_static_mount_is_not_an_api_route_so_it_cannot_dodge_the_permission_test(
    rig: Rig,
) -> None:
    schema = httpx.get(f"{rig.base}/openapi.json").json()
    assert not any(path.startswith("/app") for path in schema["paths"])
