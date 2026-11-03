"""A small bundled list of passwords that are never acceptable.

This is **not** the 100 k list the plan names: it is a few hundred entries written for this project
(facts about what people choose are not copyrightable, so no licence is involved), padded out to the
12-character minimum by the patterns people use to reach it. A larger list is a D2 improvement and
needs its licence read first. The check is a floor against the obvious, not a strength estimator.
"""

from __future__ import annotations

_BASES = (
    "password",
    "passw0rd",
    "letmein",
    "welcome",
    "qwerty",
    "qwertyuiop",
    "asdfgh",
    "asdfghjkl",
    "zxcvbnm",
    "iloveyou",
    "admin",
    "administrator",
    "changeme",
    "default",
    "secret",
    "monkey",
    "dragon",
    "football",
    "baseball",
    "master",
    "shadow",
    "sunshine",
    "princess",
    "superman",
    "batman",
    "trustno1",
    "freedom",
    "whatever",
    "starwars",
    "computer",
    "internet",
    "security",
    "camera",
    "vigil",
    "vigil88",
    "vigil-88",
    "operator",
    "viewer",
    "guard",
    "surveillance",
    "openseame",
    "access",
    "login",
    "hello",
    "abc123",
    "123456",
    "12345678",
    "123456789",
    "1234567890",
    "0987654321",
    "111111",
    "000000",
    "1q2w3e",
    "1q2w3e4r",
    "1qaz2wsx",
    "zaq12wsx",
    "michael",
    "jennifer",
    "charlie",
    "summer",
    "winter",
    "spring",
    "autumn",
    "london",
)
_SUFFIXES = (
    "",
    "1",
    "12",
    "123",
    "1234",
    "12345",
    "123456",
    "!",
    "!!",
    "1!",
    "@123",
    "2024",
    "2025",
    "2026",
    "#1",
    "000",
)

_SEQUENCES = (
    "123456789012",
    "1234567890123",
    "12345678901234",
    "qwertyuiop12",
    "qwertyuiopasdf",
    "asdfghjkl123",
    "zxcvbnm12345",
    "abcdefghijkl",
    "abcdefghijklmnop",
    "098765432109",
    "passwordpassword",
    "password1234",
    "password12345",
    "password123456",
    "passw0rd1234",
    "letmeinletmein",
    "iloveyou1234",
    "iloveyou12345",
    "welcome12345",
    "welcome123456",
    "administrator1",
    "administrator123",
    "changemechangeme",
    "changeme1234",
    "qazwsxedcrfv",
    "1qaz2wsx3edc",
    "1q2w3e4r5t6y",
    "1234qwer1234",
    "qwer1234qwer",
    "aaaaaaaaaaaa",
    "111111111111",
    "000000000000",
    "123123123123",
    "abcabcabcabc",
    "q1w2e3r4t5y6",
    "football12345",
)


def _build() -> frozenset[str]:
    out = set(_SEQUENCES)
    for base in _BASES:
        for suffix in _SUFFIXES:
            out.add(base + suffix)
            out.add(base.capitalize() + suffix)
            out.add(base.upper() + suffix)
    return frozenset(w.casefold() for w in out)


COMMON_PASSWORDS: frozenset[str] = _build()


def is_common(password: str) -> bool:
    folded = password.casefold()
    if folded in COMMON_PASSWORDS:
        return True
    # "Password1234" with decoration trimmed off both ends is still "password".
    trimmed = folded.strip("0123456789!@#$%^&*()_-+=. ")
    return bool(trimmed) and trimmed in COMMON_PASSWORDS
