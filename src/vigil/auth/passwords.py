"""Password hashing and policy (plan §7.3).

Argon2id through `argon2-cffi`. The parameters live *inside* every hash string, so raising them
later needs no migration: `needs_rehash` tells the caller to store a fresh hash at the next
successful login. Parameters are injected so the test-suite does not pay 250 ms per hash.
"""

from __future__ import annotations

from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from vigil.auth.common_passwords import is_common

MIN_LENGTH = 12
MAX_LENGTH = 128


@dataclass(frozen=True, slots=True)
class HashParams:
    memory_kib: int = 131_072  # 128 MiB
    iterations: int = 3
    parallelism: int = 1


# Tuned in D0 against the plan's target of about 250 ms. Measured on the development machine
# (p50 of 7): 64 MiB / 3 passes 100 ms, 64 MiB / 4 passes 130 ms, **128 MiB / 3 passes 209 ms**.
# That is the figure to quote, on that machine only; another CPU will differ, which is why the
# parameters travel inside every hash and a slower machine can raise them without a migration.
DEFAULT_PARAMS = HashParams()


class PasswordHasherService:
    __slots__ = ("_dummy", "_hasher")

    def __init__(self, params: HashParams = DEFAULT_PARAMS) -> None:
        self._hasher = PasswordHasher(
            time_cost=params.iterations,
            memory_cost=params.memory_kib,
            parallelism=params.parallelism,
        )
        # A real hash of a throwaway value, so verifying against "no such user" costs the same
        # as verifying against a user, and the two cannot be told apart by timing.
        self._dummy = self._hasher.hash("vigil-dummy-credential-for-timing")

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, stored: str, password: str) -> bool:
        try:
            return self._hasher.verify(stored, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, stored: str) -> bool:
        return self._hasher.check_needs_rehash(stored)

    def burn(self, password: str) -> None:
        """Spend the time a real verification would have taken."""
        self.verify(self._dummy, password)


def check_policy(password: str, *, username: str = "") -> str | None:
    """Why this password is refused, or None. The reasons are written for the person typing."""
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    if len(password) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters."
    if len(set(password)) <= 2:
        return "That password is too repetitive."
    if is_common(password):
        return "That password is on the list of passwords people pick first. Choose another."
    name = username.strip().casefold()
    if len(name) >= 3 and name in password.casefold():
        return "The password must not contain the username."
    return None
