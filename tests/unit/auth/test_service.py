"""Authentication behaviour (ADR 0016). Time is a manual clock, so windows are moved, not slept."""

from __future__ import annotations

import pytest

from tests.unit.auth.conftest import CHEAP, GOOD, SETUP_TOKEN, Rig
from vigil.auth import errors as E
from vigil.auth.lockout import MAX_LOCK_S, THRESHOLD, lock_seconds
from vigil.auth.passwords import PasswordHasherService, check_policy
from vigil.auth.roles import ROLE_PERMISSIONS, Permission, Role, can, least_role_for
from vigil.auth.service import RECOVERY_CODE_COUNT

# ------------------------------------------------------------------ setup


def test_a_fresh_store_requires_setup_and_setup_needs_the_token(rig: Rig) -> None:
    assert rig.service.setup_required()
    with pytest.raises(E.AuthError) as bad:
        rig.service.setup(token="wrong", username="alice", password=GOOD)
    assert bad.value.code == E.SETUP_NOT_ALLOWED
    assert rig.service.setup_required()  # a wrong token created nothing
    assert any(e.action == "setup.refused" for e in rig.audit.entries())


def test_setup_creates_one_administrator_and_ten_distinct_recovery_codes(rig: Rig) -> None:
    user, codes = rig.service.setup(token=SETUP_TOKEN, username="alice", password=GOOD)
    assert user.role is Role.ADMINISTRATOR and not rig.service.setup_required()
    assert len(codes) == RECOVERY_CODE_COUNT and len(set(codes)) == RECOVERY_CODE_COUNT
    assert all(len(c.replace("-", "")) == 16 for c in codes)  # 80 bits as base32


def test_setup_cannot_be_repeated_even_with_the_token(rig: Rig) -> None:
    rig.admin()
    with pytest.raises(E.AuthError) as again:
        rig.service.setup(token=SETUP_TOKEN, username="mallory", password=GOOD)
    assert again.value.code == E.SETUP_NOT_ALLOWED


def test_a_headless_service_makes_its_own_token_and_offers_it_once() -> None:
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from tests.unit.auth.conftest import ManualClock
    from vigil.auth.audit import AuditLog
    from vigil.auth.service import AuthService
    from vigil.auth.store import AccountStore

    with TemporaryDirectory() as d:
        store = AccountStore(Path(d) / "a.sqlite3")
        clock = ManualClock()
        made = AuthService(store, AuditLog(store, clock), PasswordHasherService(CHEAP), clock)
        supplied = AuthService(
            store, AuditLog(store, clock), PasswordHasherService(CHEAP), clock, setup_token="x" * 30
        )
        assert made.setup_token_to_print and len(made.setup_token_to_print) >= 24
        assert supplied.setup_token_to_print is None  # the shell already holds it
        store.close()


# ------------------------------------------------------------------ passwords


@pytest.mark.parametrize(
    ("password", "fragment"),
    [
        ("short1!", "at least 12"),
        ("a" * 129, "at most 128"),
        ("aaaaaaaaaaaaaaaa", "repetitive"),
        ("Password1234", "first"),
        ("qwertyuiop12", "first"),
        ("alice-is-the-best-1", "username"),
    ],
)
def test_the_password_policy_names_the_problem(password: str, fragment: str) -> None:
    reason = check_policy(password, username="alice")
    assert reason is not None and fragment in reason


def test_a_long_unremarkable_passphrase_is_accepted_and_has_no_composition_rule() -> None:
    assert check_policy("correct horse battery staple", username="alice") is None
    assert check_policy("all lowercase words here", username="alice") is None


def test_hashes_carry_their_parameters_so_raising_them_needs_no_migration() -> None:
    weak = PasswordHasherService(CHEAP)
    stronger = PasswordHasherService(CHEAP.__class__(memory_kib=16, iterations=2, parallelism=1))
    stored = weak.hash(GOOD)
    assert "$argon2id$" in stored and "m=8,t=1,p=1" in stored
    assert weak.verify(stored, GOOD) and not weak.verify(stored, GOOD + "x")
    assert stronger.needs_rehash(stored) and not weak.needs_rehash(stored)


def test_a_login_with_raised_parameters_upgrades_the_stored_hash(rig: Rig) -> None:
    rig.admin()
    before = rig.store.user_by_name("alice")
    assert before is not None
    rig.service._hasher = PasswordHasherService(
        CHEAP.__class__(memory_kib=16, iterations=2, parallelism=1)
    )
    rig.service.login("alice", GOOD)
    after = rig.store.user_by_name("alice")
    assert after is not None and after.password_hash != before.password_hash
    assert "m=16,t=2" in after.password_hash


# ------------------------------------------------------------------ login and lockout


def test_wrong_password_and_unknown_user_are_indistinguishable(rig: Rig) -> None:
    rig.admin()
    with pytest.raises(E.AuthError) as wrong:
        rig.service.login("alice", "not the password at all")
    with pytest.raises(E.AuthError) as unknown:
        rig.service.login("nobody-here", "not the password at all")
    assert (wrong.value.code, wrong.value.message) == (unknown.value.code, unknown.value.message)
    assert wrong.value.message == E.GENERIC_FAILURE


def test_an_unknown_user_still_pays_for_a_hash_verification(rig: Rig) -> None:
    from unittest.mock import patch

    rig.admin()
    with (
        patch.object(
            PasswordHasherService, "burn", autospec=True, side_effect=PasswordHasherService.burn
        ) as burn,
        pytest.raises(E.AuthError),
    ):
        rig.service.login("nobody-here", "whatever it is, long")
    assert burn.call_count == 1 and burn.call_args.args[1] == "whatever it is, long"


def test_the_lock_schedule_is_30_seconds_doubling_to_a_fifteen_minute_cap() -> None:
    assert [lock_seconds(n) for n in range(THRESHOLD)] == [0] * THRESHOLD
    assert [lock_seconds(n) for n in range(THRESHOLD, THRESHOLD + 5)] == [30, 60, 120, 240, 480]
    assert lock_seconds(THRESHOLD + 5) == MAX_LOCK_S == lock_seconds(500)


def _fail(rig: Rig, name: str, times: int) -> None:
    for _ in range(times):
        with pytest.raises(E.AuthError):
            rig.service.login(name, "definitely not it")


def test_five_failures_lock_the_account_and_the_wait_is_shown(rig: Rig) -> None:
    rig.admin()
    _fail(rig, "alice", THRESHOLD)
    with pytest.raises(E.AuthError) as locked:
        rig.service.login("alice", GOOD)  # even the right password is refused while locked
    assert locked.value.code == E.LOCKED_OUT and locked.value.retry_after_s == 30
    assert "30 s" in locked.value.message
    rig.advance(31)
    assert rig.service.login("alice", GOOD).principal.username == "alice"


def test_the_next_failure_after_a_lock_doubles_the_wait(rig: Rig) -> None:
    rig.admin()
    _fail(rig, "alice", THRESHOLD)
    rig.advance(31)
    _fail(rig, "alice", 1)
    with pytest.raises(E.AuthError) as second:
        rig.service.login("alice", GOOD)
    assert second.value.retry_after_s == 60


def test_success_resets_the_counter(rig: Rig) -> None:
    rig.admin()
    _fail(rig, "alice", THRESHOLD - 1)
    rig.service.login("alice", GOOD)
    _fail(rig, "alice", THRESHOLD - 1)  # would have locked had the counter survived
    assert rig.service.login("alice", GOOD)


def test_lockout_applies_to_names_that_do_not_exist_so_it_reveals_nothing(rig: Rig) -> None:
    rig.admin()
    _fail(rig, "ghost", THRESHOLD)
    with pytest.raises(E.AuthError) as ghost:
        rig.service.login("ghost", GOOD)
    _fail(rig, "alice", THRESHOLD)
    with pytest.raises(E.AuthError) as real:
        rig.service.login("alice", GOOD)
    assert ghost.value.code == real.value.code == E.LOCKED_OUT
    assert ghost.value.retry_after_s == real.value.retry_after_s


def test_the_counter_is_keyed_by_the_lower_cased_name(rig: Rig) -> None:
    rig.admin()
    _fail(rig, "ALICE", 3)
    _fail(rig, "alice", 2)
    with pytest.raises(E.AuthError) as locked:
        rig.service.login("Alice", GOOD)
    assert locked.value.code == E.LOCKED_OUT


def test_old_quiet_counters_are_forgotten_so_random_names_cannot_grow_the_table(rig: Rig) -> None:
    rig.admin()
    for i in range(20):
        _fail(rig, f"junk{i}", 1)
    assert len(rig.store.query("SELECT key FROM lockouts")) == 20
    rig.advance(25 * 3600)
    rig.service.login("alice", GOOD)  # any login prunes
    assert rig.store.query("SELECT key FROM lockouts") == []


# ------------------------------------------------------------------ sessions


def test_the_session_token_is_never_stored_only_its_hash(rig: Rig) -> None:
    _, token = rig.admin()
    stored = [str(r[0]) for r in rig.store.query("SELECT token_hash FROM sessions")]
    assert token not in stored and len(stored[0]) == 64
    assert len(token) >= 43  # 256 bits, url-safe


def test_a_valid_token_resolves_and_garbage_does_not(rig: Rig) -> None:
    principal, token = rig.admin()
    assert rig.service.authenticate(token) is not None
    assert rig.service.authenticate("garbage") is None and rig.service.authenticate("") is None
    assert principal.role is Role.ADMINISTRATOR


def test_idle_timeout_ends_the_session_and_activity_extends_it(rig: Rig) -> None:
    _, token = rig.admin()
    rig.advance(800)
    assert rig.service.authenticate(token) is not None  # still inside 15 minutes; touches it
    rig.advance(800)
    assert rig.service.authenticate(token) is not None  # the touch moved the idle deadline
    rig.advance(901)
    assert rig.service.authenticate(token) is None
    assert any(e.action == "session.expired" for e in rig.audit.entries())
    rig.advance(1)
    assert rig.service.authenticate(token) is None  # and it is gone, not merely refused


def test_the_absolute_lifetime_ends_a_session_that_never_goes_idle(rig: Rig) -> None:
    _, token = rig.admin()
    started = rig.clock.wall_utc()
    ended_after_s = None
    for _ in range(60):  # activity every 14 minutes: never idle, so only the ceiling can end it
        rig.advance(14 * 60)
        if rig.service.authenticate(token) is None:
            ended_after_s = (rig.clock.wall_utc() - started).total_seconds()
            break
    assert ended_after_s is not None, "a session outlived the absolute lifetime"
    assert 12 * 3600 <= ended_after_s < 12 * 3600 + 14 * 60


def test_logout_deletes_the_session_server_side(rig: Rig) -> None:
    principal, token = rig.admin()
    rig.service.logout(principal)
    assert rig.service.authenticate(token) is None
    assert rig.store.query("SELECT 1 FROM sessions") == []


def test_lock_and_unlock_keep_the_session_but_gate_it(rig: Rig) -> None:
    principal, token = rig.admin()
    rig.service.lock(principal)
    locked = rig.service.authenticate(token)
    assert locked is not None and locked.locked
    with pytest.raises(E.AuthError):
        rig.service.unlock(locked, "wrong wrong wrong")
    rig.service.unlock(locked, GOOD)
    unlocked = rig.service.authenticate(token)
    assert unlocked is not None and not unlocked.locked


def test_failed_unlocks_count_toward_the_same_lockout_as_logins(rig: Rig) -> None:
    principal, token = rig.admin()
    rig.service.lock(principal)
    locked = rig.service.authenticate(token)
    assert locked is not None
    for _ in range(THRESHOLD):
        with pytest.raises(E.AuthError):
            rig.service.unlock(locked, "wrong wrong wrong")
    with pytest.raises(E.AuthError) as refused:
        rig.service.login("alice", GOOD)
    assert refused.value.code == E.LOCKED_OUT


# ------------------------------------------------------------------ roles and users


def test_the_permission_table_matches_the_plan() -> None:
    assert ROLE_PERMISSIONS[Role.VIEWER] == {Permission.VIEW}
    assert ROLE_PERMISSIONS[Role.OPERATOR] == {
        Permission.VIEW,
        Permission.ACT_ON_INCIDENTS,
        Permission.MANAGE_MUTES,
        Permission.EXPORT,
    }
    assert ROLE_PERMISSIONS[Role.ADMINISTRATOR] == set(Permission)
    assert not can(Role.OPERATOR, Permission.CONFIGURE)
    assert least_role_for(Permission.VIEW) is Role.VIEWER
    assert least_role_for(Permission.ACT_ON_INCIDENTS) is Role.OPERATOR
    assert least_role_for(Permission.MANAGE_USERS) is Role.ADMINISTRATOR


def test_the_last_administrator_cannot_be_deleted_demoted_or_disabled(rig: Rig) -> None:
    admin, _ = rig.admin()
    for action in (
        lambda: rig.service.delete_user(admin, admin.user_id),
        lambda: rig.service.set_role(admin, admin.user_id, Role.VIEWER),
        lambda: rig.service.set_disabled(admin, admin.user_id, True),
    ):
        with pytest.raises(E.AuthError) as refused:
            action()
        assert refused.value.code == E.CONFLICT


def test_with_a_second_administrator_the_first_can_be_demoted(rig: Rig) -> None:
    admin, _ = rig.admin()
    second = rig.user(admin, "bob", Role.ADMINISTRATOR)
    rig.service.set_role(admin, admin.user_id, Role.OPERATOR)
    assert rig.service.list_users() and rig.store.user_by_id(second) is not None


def test_a_role_change_or_disable_ends_that_users_sessions_immediately(rig: Rig) -> None:
    admin, _ = rig.admin()
    uid = rig.user(admin, "carol")
    token = rig.service.login("carol", GOOD + "!").token
    rig.service.set_role(admin, uid, Role.VIEWER)
    assert rig.service.authenticate(token) is None
    token = rig.service.login("carol", GOOD + "!").token
    rig.service.set_disabled(admin, uid, True)
    assert rig.service.authenticate(token) is None
    with pytest.raises(E.AuthError):
        rig.service.login("carol", GOOD + "!")


def test_usernames_are_unique_case_insensitively_and_validated(rig: Rig) -> None:
    admin, _ = rig.admin()
    with pytest.raises(E.AuthError) as dup:
        rig.service.create_user(admin, username="ALICE", password=GOOD + "!", role=Role.VIEWER)
    assert dup.value.code == E.CONFLICT
    with pytest.raises(E.AuthError):
        rig.service.create_user(admin, username="bad name!", password=GOOD + "!", role=Role.VIEWER)


def test_an_admin_reset_forces_a_change_and_ends_sessions(rig: Rig) -> None:
    admin, _ = rig.admin()
    uid = rig.user(admin, "dave")
    token = rig.service.login("dave", GOOD + "!").token
    rig.service.reset_password(admin, uid, "a brand new passphrase")
    assert rig.service.authenticate(token) is None
    result = rig.service.login("dave", "a brand new passphrase")
    assert result.principal.must_change_password


def test_changing_your_own_password_keeps_this_session_and_ends_the_others(rig: Rig) -> None:
    principal, here = rig.admin()
    elsewhere = rig.service.login("alice", GOOD).token
    rig.service.change_password(principal, GOOD, "another long passphrase")
    assert rig.service.authenticate(here) is not None
    assert rig.service.authenticate(elsewhere) is None
    with pytest.raises(E.AuthError):
        rig.service.change_password(principal, "wrong current one", "yet another passphrase")


# ------------------------------------------------------------------ recovery


def test_a_recovery_code_resets_the_password_once(rig: Rig) -> None:
    _, codes = rig.service.setup(token=SETUP_TOKEN, username="alice", password=GOOD)
    rig.service.recover("alice", codes[0].lower().replace("-", " "), "recovered passphrase!")
    assert rig.service.login("alice", "recovered passphrase!")
    with pytest.raises(E.AuthError) as reuse:
        rig.service.recover("alice", codes[0], "second reuse passphrase")
    assert reuse.value.code == E.INVALID_CODE
    rig.service.recover("alice", codes[1], "another recovered one!")  # the others still work


def test_a_wrong_code_is_refused_and_counts_toward_lockout(rig: Rig) -> None:
    rig.service.setup(token=SETUP_TOKEN, username="alice", password=GOOD)
    for _ in range(THRESHOLD):
        with pytest.raises(E.AuthError) as bad:
            rig.service.recover("alice", "AAAA-BBBB-CCCC-DDDD", "recovered passphrase!")
        assert bad.value.code == E.INVALID_CODE
    with pytest.raises(E.AuthError) as locked:
        rig.service.recover("alice", "AAAA-BBBB-CCCC-DDDD", "recovered passphrase!")
    assert locked.value.code == E.LOCKED_OUT


def test_regenerating_codes_invalidates_the_old_set(rig: Rig) -> None:
    principal, _ = rig.admin()
    with rig.store.transaction():
        pass
    old = rig.store.unused_recovery_codes(principal.user_id)
    new_codes = rig.service.regenerate_recovery_codes(principal)
    assert len(new_codes) == RECOVERY_CODE_COUNT
    assert {i for i, _ in rig.store.unused_recovery_codes(principal.user_id)}.isdisjoint(
        {i for i, _ in old}
    )


def test_reset_admin_removes_administrators_keeps_others_and_requires_setup_again(rig: Rig) -> None:
    admin, token = rig.admin()
    rig.user(admin, "erin", Role.OPERATOR)
    new_token = rig.service.reset_admin()
    assert rig.service.setup_required() and rig.service.authenticate(token) is None
    assert rig.store.user_by_name("erin") is not None and rig.store.user_by_name("alice") is None
    rig.service.setup(token=new_token, username="alice2", password=GOOD)
    assert any(e.action == "admin.reset" for e in rig.audit.entries())


# ------------------------------------------------------------------ audit chain


def test_every_security_event_is_audited_with_the_acting_user(rig: Rig) -> None:
    admin, _ = rig.admin()
    rig.user(admin, "frank")
    _fail(rig, "alice", 1)
    actions = [e.action for e in rig.audit.entries(limit=50)]
    for expected in ("setup.completed", "login.success", "user.created", "login.failure"):
        assert expected in actions
    created = next(e for e in rig.audit.entries() if e.action == "user.created")
    assert created.username == "alice" and '"role": "operator"' in created.detail


def test_the_chain_verifies_and_editing_a_row_is_reported_at_that_row(rig: Rig) -> None:
    rig.admin()
    _fail(rig, "alice", 2)
    assert rig.audit.verify().ok
    target = rig.audit.entries()[1]
    rig.store.run("UPDATE audit SET action = 'nothing.to.see' WHERE seq = ?", (target.seq,))
    report = rig.audit.verify()
    assert not report.ok and report.first_bad_seq == target.seq


def test_deleting_a_row_from_the_middle_breaks_the_chain(rig: Rig) -> None:
    rig.admin()
    _fail(rig, "alice", 3)
    middle = rig.audit.entries()[2]
    rig.store.run("DELETE FROM audit WHERE seq = ?", (middle.seq,))
    report = rig.audit.verify()
    assert not report.ok and report.first_bad_seq == middle.seq + 1


def test_audit_entries_page_newest_first_and_filter_by_action(rig: Rig) -> None:
    rig.admin()
    _fail(rig, "alice", 4)
    page = rig.audit.entries(limit=3)
    assert [e.seq for e in page] == sorted((e.seq for e in page), reverse=True)
    older = rig.audit.entries(limit=100, before_seq=page[-1].seq)
    assert all(e.seq < page[-1].seq for e in older)
    assert {e.action for e in rig.audit.entries(action="login.failure")} == {"login.failure"}


def test_an_oversized_detail_is_truncated_not_stored(rig: Rig) -> None:
    entry = rig.audit.record("test.big", detail={"x": "y" * 10_000})
    assert '"truncated": true' in entry.detail.replace(":true", ": true")
    assert rig.audit.verify().ok


def test_the_account_database_is_a_separate_file_with_a_clean_integrity_check(rig: Rig) -> None:
    rig.admin()
    assert rig.store.integrity_ok()
