"""L4 temporal engine (04 §6, D-007): observations in, accumulated belief out.

This engine owns time, doubt and confirmation. A detection module answers one question about
one frame; everything about *whether that evidence adds up* lives here, once, so that every
event category gets the same temporal behaviour and a bug is fixed in one place (06 §2).

Fails **closed**: an accumulator that cannot be reasoned over emits no candidate.
"""
