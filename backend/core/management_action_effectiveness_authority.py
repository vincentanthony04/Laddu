"""Evidence-honest management-action effectiveness attribution.

This authority measures what happened *after* a governed Model-Paper lifecycle
action using only ordered observed prices.  It deliberately separates:

* within-position observations, which are available from the Rich Signal Ledger;
* post-exit counterfactual observations, which require an explicit canonical
  price path supplied by a separate market-data authority.

No causal claim is made.  Missing post-exit evidence remains missing rather than
being replaced by a hypothetical price path.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping


class ManagementActionEffectivenessAuthority:
    authority = "ManagementActionEffectivenessAuthority"
    authority_version = "1.0.0"

    @staticmethod
    def _float(value: Any) -> float | None:
        try:
            out = float(value)
            return out if math.isfinite(out) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _dt(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return out if out.tzinfo else out.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    @classmethod
    def _classify(cls, event: Mapping[str, Any]) -> str:
        event_type = str(event.get("event_type") or "").upper()
        action = str(event.get("action") or "").upper()
        reason = str(event.get("reason") or "").upper()
        thesis = str(event.get("thesis_state") or "").upper()
        joined = " | ".join((action, reason, thesis))
        if event_type == "SETTLED" or any(token in joined for token in ("EXIT", "TARGET_HIT", "STOP_HIT", "CLOSED")):
            if "INVALID" in joined:
                return "INVALIDATED_EXIT"
            if "WEAK" in joined:
                return "WEAKENING_EXIT"
            if "TIME" in joined:
                return "TIME_EXIT"
            return "EXIT"
        if "TRAIL" in joined:
            return "TRAIL"
        if "BREAKEVEN" in joined or "PROTECT" in joined:
            return "PROTECT"
        if "WAIT" in joined or "PRESSURE" in joined or "DO NOT ADD" in joined:
            return "WAIT"
        if "HOLD" in joined or event_type == "REASSESSED":
            return "HOLD"
        if event_type == "OPENED" or "ENTER" in joined:
            return "ENTER"
        return "OTHER"

    @classmethod
    def _normalise_path(cls, values: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in values or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            stamp = cls._dt(row.get("occurred_at") or row.get("timestamp") or row.get("ts"))
            price = cls._float(row.get("price") if row.get("price") is not None else row.get("close"))
            row["_dt"] = stamp
            row["price"] = price
            row["action_class"] = cls._classify(row)
            out.append(row)
        out.sort(key=lambda item: (item.get("_dt") or datetime.min.replace(tzinfo=timezone.utc)))
        return out

    @staticmethod
    def _side_move(side: str, start: float, end: float) -> float:
        return (end - start) if side == "LONG" else (start - end)

    @classmethod
    def enrich(cls, record: Mapping[str, Any], *, post_exit_path: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        row = dict(record or {})
        side = str(row.get("side") or "").upper()
        entry = cls._float(row.get("entry_price") if row.get("entry_price") is not None else row.get("original_entry"))
        original_stop = cls._float(row.get("original_stop"))
        if side not in {"LONG", "SHORT"} or entry is None or original_stop is None:
            row["management_action_effectiveness"] = {
                "authority": cls.authority,
                "authority_version": cls.authority_version,
                "state": "UNSCORABLE_IDENTITY_OR_RISK",
                "actions": [],
                "causal_claim": False,
            }
            return row
        initial_r = abs(entry - original_stop)
        if initial_r <= 0:
            row["management_action_effectiveness"] = {
                "authority": cls.authority,
                "authority_version": cls.authority_version,
                "state": "UNSCORABLE_ZERO_INITIAL_RISK",
                "actions": [],
                "causal_claim": False,
            }
            return row

        path = cls._normalise_path(row.get("lifecycle_action_path") if isinstance(row.get("lifecycle_action_path"), list) else [])
        post = cls._normalise_path(post_exit_path if post_exit_path is not None else row.get("post_exit_management_path"))
        actions: list[dict[str, Any]] = []
        for index, event in enumerate(path):
            # REASSESSED is evidence about thesis state, not itself a portfolio
            # management action. Its price remains in ``path`` so a preceding
            # MANAGED action may use it as a later observed price.
            if str(event.get("event_type") or "").upper() == "REASSESSED":
                continue
            action_class = event["action_class"]
            price = event.get("price")
            managed_stop = cls._float(event.get("managed_stop"))
            secured_r = None
            if managed_stop is not None:
                if side == "LONG":
                    secured_r = max(0.0, managed_stop - original_stop) / initial_r
                else:
                    secured_r = max(0.0, original_stop - managed_stop) / initial_r

            later = [item for item in path[index + 1:] if item.get("price") is not None]
            later_prices = [float(item["price"]) for item in later]
            within_state = "OBSERVED_WITHIN_POSITION" if price is not None and later_prices else "INSUFFICIENT_LATER_OBSERVED_PRICE"
            subsequent_final_r = subsequent_best_r = subsequent_worst_r = None
            if price is not None and later_prices:
                moves = [cls._side_move(side, float(price), value) / initial_r for value in later_prices]
                subsequent_final_r = moves[-1]
                subsequent_best_r = max(moves)
                subsequent_worst_r = min(moves)

            is_exit = action_class in {"EXIT", "INVALIDATED_EXIT", "WEAKENING_EXIT", "TIME_EXIT"}
            observed_avoided_loss_r = None
            observed_lost_upside_r = None
            post_exit_state = "NOT_APPLICABLE_PRE_EXIT_ACTION"
            if is_exit:
                post_prices = [float(item["price"]) for item in post if item.get("price") is not None]
                if price is None:
                    post_exit_state = "EXIT_PRICE_MISSING"
                elif not post_prices:
                    post_exit_state = "POST_EXIT_PATH_REQUIRED"
                else:
                    post_moves = [cls._side_move(side, float(price), value) / initial_r for value in post_prices]
                    # Observational opportunity-cost/downside only. These values
                    # are not causal estimates of what an unexecuted policy would
                    # have earned because fill/risk state after exit is unknown.
                    observed_lost_upside_r = max(0.0, max(post_moves))
                    observed_avoided_loss_r = max(0.0, -min(post_moves))
                    post_exit_state = "OBSERVED_POST_EXIT_PATH"

            signal_age = event.get("signal_age") if isinstance(event.get("signal_age"), Mapping) else {}
            actions.append({
                "event_type": event.get("event_type"),
                "action": event.get("action"),
                "action_class": action_class,
                "reason": event.get("reason"),
                "thesis_state": event.get("thesis_state"),
                "occurred_at": event.get("occurred_at"),
                "price": price,
                "managed_stop": managed_stop,
                "secured_r": round(secured_r, 6) if secured_r is not None else None,
                "subsequent_observation_state": within_state,
                "subsequent_final_r": round(subsequent_final_r, 6) if subsequent_final_r is not None else None,
                "subsequent_best_r": round(subsequent_best_r, 6) if subsequent_best_r is not None else None,
                "subsequent_worst_r": round(subsequent_worst_r, 6) if subsequent_worst_r is not None else None,
                "post_exit_evidence_state": post_exit_state,
                "observed_avoided_loss_r": round(observed_avoided_loss_r, 6) if observed_avoided_loss_r is not None else None,
                "observed_lost_upside_r": round(observed_lost_upside_r, 6) if observed_lost_upside_r is not None else None,
                "generation_age_seconds": signal_age.get("generation_age_seconds"),
                "open_age_seconds": signal_age.get("open_age_seconds"),
                "regime": event.get("regime"),
                "full_thesis_validated": event.get("full_thesis_validated"),
                "causal_claim": False,
            })

        scorable = [item for item in actions if item.get("subsequent_observation_state") == "OBSERVED_WITHIN_POSITION"]
        exit_actions = [item for item in actions if item.get("action_class") in {"EXIT", "INVALIDATED_EXIT", "WEAKENING_EXIT", "TIME_EXIT"}]
        post_exit_scored = [item for item in exit_actions if item.get("post_exit_evidence_state") == "OBSERVED_POST_EXIT_PATH"]
        row["management_action_effectiveness"] = {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "state": "READY" if actions else "NO_LIFECYCLE_ACTION_EVIDENCE",
            "actions": actions,
            "action_count": len(actions),
            "within_position_scorable_count": len(scorable),
            "exit_action_count": len(exit_actions),
            "post_exit_scorable_count": len(post_exit_scored),
            "post_exit_coverage_pct": round(100.0 * len(post_exit_scored) / len(exit_actions), 3) if exit_actions else None,
            "causal_claim": False,
            "policy": "ordered observed lifecycle prices only; post-exit avoided-loss/lost-upside remain unavailable unless an explicit canonical post-exit price path is supplied",
        }
        return row

    @classmethod
    def aggregate(cls, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        actions: list[Mapping[str, Any]] = []
        for row in rows or []:
            block = row.get("management_action_effectiveness") if isinstance(row, Mapping) else None
            if isinstance(block, Mapping):
                actions.extend(item for item in (block.get("actions") or []) if isinstance(item, Mapping))
        by_class: dict[str, dict[str, Any]] = {}
        for action in actions:
            key = str(action.get("action_class") or "OTHER")
            group = by_class.setdefault(key, {"observed": 0, "scorable": 0, "post_exit_scorable": 0, "subsequent_final_r": [], "secured_r": [], "avoided_loss_r": [], "lost_upside_r": []})
            group["observed"] += 1
            if action.get("subsequent_observation_state") == "OBSERVED_WITHIN_POSITION":
                group["scorable"] += 1
            if action.get("post_exit_evidence_state") == "OBSERVED_POST_EXIT_PATH":
                group["post_exit_scorable"] += 1
            for source, target in (("subsequent_final_r", "subsequent_final_r"), ("secured_r", "secured_r"), ("observed_avoided_loss_r", "avoided_loss_r"), ("observed_lost_upside_r", "lost_upside_r")):
                value = cls._float(action.get(source))
                if value is not None:
                    group[target].append(value)
        out: dict[str, Any] = {}
        for key, group in sorted(by_class.items()):
            def mean(name: str) -> float | None:
                values = group[name]
                return round(sum(values) / len(values), 6) if values else None
            out[key] = {
                "observed": group["observed"],
                "within_position_scorable": group["scorable"],
                "post_exit_scorable": group["post_exit_scorable"],
                "average_subsequent_final_r": mean("subsequent_final_r"),
                "average_secured_r": mean("secured_r"),
                "average_observed_avoided_loss_r": mean("avoided_loss_r"),
                "average_observed_lost_upside_r": mean("lost_upside_r"),
            }
        return {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "actions_observed": len(actions),
            "by_action_class": out,
            "causal_claim": False,
        }


DEFAULT_MANAGEMENT_ACTION_EFFECTIVENESS_AUTHORITY = ManagementActionEffectivenessAuthority()
