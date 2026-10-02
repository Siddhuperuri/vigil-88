"""L4 ingest engine: sources, capture worker, buffers, camera health.

Nothing here imports cv2 or numpy at package import time; only the concrete sources do,
and only when the `capture` extra is installed.
"""
