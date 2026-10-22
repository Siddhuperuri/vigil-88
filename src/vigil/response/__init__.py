"""L4 response engine: verified events become incidents, evidence and alerts (01 §6).

This engine fails **open**, and it is the only one that does. Temporal and verification fail
closed because a false alarm spends operator trust; response fails open because losing the
record of a real incident is worse than an incomplete record. So an incident is persisted even
when evidence capture or alerting fails, and it is marked partial rather than quietly perfect.
"""
