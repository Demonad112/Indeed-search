"""jobpipe — semi-autonomous job discovery and application pipeline."""

__version__ = "0.1.0"

# Statuses a job may hold. Mirrored by a CHECK constraint in db.py.
STATUSES = (
    "new",
    "scored",
    "drafted",
    "approved",
    "submitted",
    "rejected",
    "expired",
)

# A job must miss this many consecutive successful runs of its own source
# before we call it expired.
EXPIRE_AFTER_MISSES = 7
