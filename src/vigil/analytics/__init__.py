"""L5 analytics: aggregations, reports and exports over what was actually persisted.

Every number this layer produces comes from a row in the database. Nothing is modelled,
extrapolated or inferred from a partial window, and a metric that cannot be computed from the
records present is reported as unavailable rather than as zero — because on a dashboard a zero
and an absence look identical, and only one of them is true.

The distinction the whole layer is organised around (06 §1):

    raw observations -> event candidates -> verified events
        -> persisted incidents -> operator actions

Those are five different populations and conflating any two of them produces a number that
sounds meaningful and is not. `queries.py` keeps them apart by construction: incidents come from
`incidents`, rejections from `candidate_events`, and dismissals from the operator's own
transitions.
"""
