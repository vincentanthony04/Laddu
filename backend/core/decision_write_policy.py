"""Compatibility shim; use :mod:`decision_write_dedup_filter` for new code."""
from core.decision_write_dedup_filter import DecisionWriteDedupFilter

# Historical import preserved for external/test compatibility.  The old name
# was misleading: this class is a persistence dedup filter, not policy authority.
DecisionWritePolicy = DecisionWriteDedupFilter

__all__ = ["DecisionWritePolicy", "DecisionWriteDedupFilter"]
