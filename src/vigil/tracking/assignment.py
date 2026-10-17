"""Detection-to-track assignment (04 §3.2, 02 §2 `tracking/assignment.py`).

Greedy matching over a similarity matrix: the best remaining pair is taken, both of its row
and column are struck out, and the search repeats. Ties are broken by (row, column), so the
result depends only on the numbers and never on dict or set ordering (09 §9).

Greedy rather than Hungarian, deliberately. Hungarian minimises the *total* cost, which is the
right objective when every pair must be assigned; here most pairs are forbidden outright by
`min_similarity`, the matrix is tiny (tens of tracks against tens of detections), and the
failure Hungarian prevents — sacrificing a confident pair to improve a marginal one — is
exactly the trade that produces ID switches. Greedy keeps the confident pair. If measurement
ever shows the total-cost objective matters here, this module is the one file that changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

Matrix = Sequence[Sequence[float]]


@dataclass(frozen=True, slots=True)
class Assignment:
    """`matches` holds (row, column) pairs; the unmatched tuples are sorted."""

    matches: tuple[tuple[int, int], ...]
    unmatched_rows: tuple[int, ...]
    unmatched_columns: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "matches", tuple(self.matches))
        object.__setattr__(self, "unmatched_rows", tuple(self.unmatched_rows))
        object.__setattr__(self, "unmatched_columns", tuple(self.unmatched_columns))


def greedy_match(
    similarity: Matrix, *, min_similarity: float, rows: int, columns: int
) -> Assignment:
    """Match rows to columns, strongest pair first.

    `rows` and `columns` are passed explicitly so an empty matrix still reports the right
    unmatched sets: `len(similarity)` is 0 both when there are no rows and when the caller
    handed in a degenerate matrix, and those mean different things to the tracker.
    """
    candidates = [
        (-similarity[r][c], r, c)
        for r in range(rows)
        for c in range(columns)
        if similarity[r][c] >= min_similarity
    ]
    candidates.sort()

    taken_rows: set[int] = set()
    taken_columns: set[int] = set()
    matches: list[tuple[int, int]] = []
    for _negated, r, c in candidates:
        if r in taken_rows or c in taken_columns:
            continue
        taken_rows.add(r)
        taken_columns.add(c)
        matches.append((r, c))

    return Assignment(
        matches=tuple(matches),
        unmatched_rows=tuple(r for r in range(rows) if r not in taken_rows),
        unmatched_columns=tuple(c for c in range(columns) if c not in taken_columns),
    )
