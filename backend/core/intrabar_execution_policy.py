"""Canonical OHLC intrabar ambiguity policy for historical execution.

Completed OHLC bars do not reveal whether a stop or target traded first when
both lie inside the same bar.  Production-faithful historical evidence must not
silently choose the favourable path.  This authority owns that one ambiguity
rule for exact replay and vectorised acceleration; live Model Paper quote
sequences are ordered observations and therefore do not use this policy.
"""
from __future__ import annotations

from typing import Any, Dict


class IntrabarExecutionPolicy:
    authority = "IntrabarExecutionPolicy"
    authority_version = "1.0.0"
    production_policy = "CONSERVATIVE_STOP_FIRST"

    @classmethod
    def resolve(cls, *, stop_hit: bool, target_hit: bool, conservative: bool = True) -> Dict[str, Any]:
        stop_hit = bool(stop_hit)
        target_hit = bool(target_hit)
        if stop_hit and target_hit:
            # ``conservative=False`` is retained only for explicit research
            # sensitivity comparisons. Production/default evidence is stop-first.
            outcome = "STOP" if conservative else "TARGET"
            return {
                "outcome": outcome,
                "ambiguous": True,
                "state": "AMBIGUOUS_INTRABAR_STOP_FIRST" if conservative else "AMBIGUOUS_INTRABAR_RESEARCH_TARGET_FIRST",
                "authority": cls.authority,
                "authority_version": cls.authority_version,
                "production_eligible": bool(conservative),
                "policy": cls.production_policy,
            }
        if stop_hit:
            outcome, state = "STOP", "STOP_ONLY"
        elif target_hit:
            outcome, state = "TARGET", "TARGET_ONLY"
        else:
            outcome, state = None, "NO_TOUCH"
        return {
            "outcome": outcome,
            "ambiguous": False,
            "state": state,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "production_eligible": True,
            "policy": cls.production_policy,
        }


DEFAULT_INTRABAR_EXECUTION_POLICY = IntrabarExecutionPolicy()
