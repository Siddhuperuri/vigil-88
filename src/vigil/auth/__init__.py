"""Local accounts, roles, sessions and the audit chain (ADR 0016).

This package is imported only by `api/` and `cli/`. The decision engines and `pipeline/` never
import it; an actor reaches `response/` as a plain string parameter. `import-linter` enforces the
direction.
"""
