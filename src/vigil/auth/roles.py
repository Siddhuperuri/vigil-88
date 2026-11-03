"""Roles and what each may do (plan §7.7).

A permission names an *ability*, not a route. `api/policy.py` maps every route to exactly one of
them, and a test fails the build when a route is missing, so a new endpoint cannot ship open by
accident.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class Role(StrEnum):
    ADMINISTRATOR = "administrator"
    OPERATOR = "operator"
    VIEWER = "viewer"


class Permission(StrEnum):
    VIEW = "view"
    ACT_ON_INCIDENTS = "act_on_incidents"
    MANAGE_MUTES = "manage_mutes"
    EXPORT = "export"
    CONFIGURE = "configure"
    MANAGE_USERS = "manage_users"
    VIEW_AUDIT = "view_audit"
    DIAGNOSTICS = "diagnostics"


_OPERATOR = frozenset(
    {Permission.VIEW, Permission.ACT_ON_INCIDENTS, Permission.MANAGE_MUTES, Permission.EXPORT}
)

ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = MappingProxyType(
    {
        Role.ADMINISTRATOR: frozenset(Permission),
        Role.OPERATOR: _OPERATOR,
        Role.VIEWER: frozenset({Permission.VIEW}),
    }
)

# The smallest role that holds each permission, for the "requires <role>" reason shown to a user.
_LEAST: tuple[Role, ...] = (Role.VIEWER, Role.OPERATOR, Role.ADMINISTRATOR)


def can(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]


def least_role_for(permission: Permission) -> Role:
    return next(r for r in _LEAST if can(r, permission))
