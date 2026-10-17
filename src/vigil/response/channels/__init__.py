"""Alert channels (04 §3.6). Each reports `is_available()` honestly.

An unavailable channel is absent from the UI, not stubbed: a channel that accepted a send and
discarded it would be the worst kind of fake functionality, because the operator would believe
someone had been told.
"""
