"""L4 event engine (04 §3.5, 06): a scene in, observations and candidates out.

A module answers one narrow question about one frame — how strongly does this scene support
event X for subject Y — and the platform owns time, doubt and consequence (D-003). There is no
path from a module to an incident, which is the single most important property of this model.

This package therefore contains no temporal reasoning. It runs modules, and it builds a
`CandidateEvent` from an accumulator state the temporal engine hands it. `pipeline/` wires the
two together, because sibling engines must not import each other (01 §5).

Fails **closed** per module: a module that raises is quarantined, and the rest of the pipeline
does not notice.
"""
