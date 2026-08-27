"""First-class signal-age authority for Project Laddu Level-5 learning.

Age is evidence, never a reason to invent confidence. Automatic age-based
risk changes are disabled unless an explicitly approved, versioned policy is
carried with the frozen decision. Approved policies may only reduce risk.

The same versioned attribution buckets are used by live Final projection,
settled Performance and governed Learning. Missing timestamps are represented
explicitly and are never guessed from unrelated clocks.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


class SignalAgeAuthority:
    authority = "SignalAgeAuthority"
    authority_version = "1.2.0-causal-time"
    default_policy_version = "signal-age-measure-only-1.0.0"
    attribution_policy_version = "signal-age-attribution-buckets-1.0.0"

    # Analytics-only bins. These labels do not change trading risk by
    # themselves. Any risk adjustment remains governed by approved_policy.
    ATTRIBUTION_BUCKETS = (
        ("0_5M", 0.0, 300.0),
        ("5_15M", 300.0, 900.0),
        ("15_60M", 900.0, 3600.0),
        ("1_4H", 3600.0, 14400.0),
        ("4H_PLUS", 14400.0, None),
    )

    @staticmethod
    def _parse(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except Exception:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def bucket(cls, seconds: Any) -> str:
        if seconds is None:
            return "MISSING"
        try:
            value = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return "MISSING"
        for label, minimum, maximum in cls.ATTRIBUTION_BUCKETS:
            if value < minimum:
                continue
            if maximum is None or value < maximum:
                return label
        return "MISSING"

    @classmethod
    def measure(
        cls,
        *,
        generated_at: Any = None,
        opened_at: Any = None,
        at: Any = None,
        mode: Any = None,
        approved_policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = cls._parse(at) or datetime.now(timezone.utc)
        generated = cls._parse(generated_at)
        opened = cls._parse(opened_at)
        missing = []
        if generated is None:
            missing.append("generated_at")
        if opened is None:
            missing.append("opened_at")

        causal_violations = []
        if generated is not None and now < generated:
            causal_violations.append("as_of_before_generated_at")
        if generated is not None and opened is not None and opened < generated:
            causal_violations.append("opened_at_before_generated_at")
        if opened is not None and now < opened:
            causal_violations.append("as_of_before_opened_at")

        if causal_violations:
            generation_age = None
            open_age = None
            decision_delay = None
            attribution_state = "INVALID"
        else:
            generation_age = (now - generated).total_seconds() if generated else None
            open_age = (now - opened).total_seconds() if opened else None
            decision_delay = (opened - generated).total_seconds() if generated and opened else None
            attribution_state = "COMPLETE" if not missing else ("PARTIAL" if len(missing) == 1 else "MISSING")

        policy = dict(approved_policy or {})
        approved = (
            policy.get("human_approved") is True
            and bool(str(policy.get("policy_version") or "").strip())
            and isinstance(policy.get("bins"), list)
        )
        multiplier = 1.0
        matched_bin = None
        if approved and generation_age is not None:
            for item in sorted((dict(x) for x in policy.get("bins") or []), key=lambda x: float(x.get("min_age_seconds") or 0)):
                minimum = max(0.0, float(item.get("min_age_seconds") or 0.0))
                maximum = item.get("max_age_seconds")
                maximum = None if maximum is None else max(minimum, float(maximum))
                if generation_age < minimum or (maximum is not None and generation_age >= maximum):
                    continue
                # Governance invariant: age adjustment can de-risk only.
                requested = float(item.get("risk_multiplier") or 1.0)
                multiplier = max(0.0, min(1.0, requested))
                matched_bin = {
                    "min_age_seconds": minimum,
                    "max_age_seconds": maximum,
                    "risk_multiplier": multiplier,
                }
                break

        generation_age = round(generation_age, 3) if generation_age is not None else None
        open_age = round(open_age, 3) if open_age is not None else None
        decision_delay = round(decision_delay, 3) if decision_delay is not None else None
        return {
            "generated_at": generated.isoformat().replace("+00:00", "Z") if generated else None,
            "opened_at": opened.isoformat().replace("+00:00", "Z") if opened else None,
            "as_of": now.isoformat().replace("+00:00", "Z"),
            "generation_age_seconds": generation_age,
            "open_age_seconds": open_age,
            "decision_delay_seconds": decision_delay,
            "generation_age_bucket": cls.bucket(generation_age),
            "open_age_bucket": cls.bucket(open_age),
            "decision_delay_bucket": cls.bucket(decision_delay),
            "age_attribution_state": attribution_state,
            "age_attribution_missing": missing,
            "causal_violations": causal_violations,
            "causal_valid": not causal_violations,
            "age_bucket_policy_version": cls.attribution_policy_version,
            "mode": str(mode or "").lower() or None,
            "age_risk_multiplier": multiplier,
            "age_risk_state": "APPROVED_DE_RISK" if approved and multiplier < 1.0 else ("APPROVED_NEUTRAL" if approved else "MEASURE_ONLY"),
            "age_risk_policy_version": str(policy.get("policy_version") or cls.default_policy_version),
            "matched_age_bin": matched_bin,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "policy": "signal age is first-class evidence; attribution buckets are analytics-only; automatic age risk policy must be human-approved, versioned and may only reduce risk",
        }

    @classmethod
    def enrich(
        cls,
        row: Mapping[str, Any],
        *,
        at: Any = None,
    ) -> dict[str, Any]:
        out = dict(row or {})
        existing = out.get("signal_age") if isinstance(out.get("signal_age"), Mapping) else None
        if existing and existing.get("age_bucket_policy_version") == cls.attribution_policy_version:
            age = dict(existing)
        else:
            age = cls.measure(
                generated_at=out.get("generated_at"),
                opened_at=out.get("opened_at"),
                at=at or out.get("closed_at") or out.get("updated_at"),
                mode=out.get("mode"),
            )
        out["signal_age"] = age
        for key in (
            "generation_age_seconds", "open_age_seconds", "decision_delay_seconds",
            "generation_age_bucket", "open_age_bucket", "decision_delay_bucket",
            "age_attribution_state", "age_bucket_policy_version",
        ):
            out[key] = age.get(key)
        return out

    @classmethod
    def aggregate(cls, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        enriched = [cls.enrich(row) for row in (rows or ())]
        generation = Counter(str(row.get("generation_age_bucket") or "MISSING") for row in enriched)
        opened = Counter(str(row.get("open_age_bucket") or "MISSING") for row in enriched)
        delay = Counter(str(row.get("decision_delay_bucket") or "MISSING") for row in enriched)
        complete = sum(row.get("age_attribution_state") == "COMPLETE" for row in enriched)
        missing = len(enriched) - complete

        def average(key: str) -> float | None:
            values = []
            for row in enriched:
                try:
                    value = row.get(key)
                    if value is not None:
                        values.append(float(value))
                except (TypeError, ValueError):
                    continue
            return round(sum(values) / len(values), 3) if values else None

        return {
            "observations": len(enriched),
            "complete_age_observations": complete,
            "missing_age_observations": missing,
            "missing_age_explicit": True,
            "generation_age_buckets": dict(sorted(generation.items())),
            "open_age_buckets": dict(sorted(opened.items())),
            "decision_delay_buckets": dict(sorted(delay.items())),
            "average_generation_age_seconds": average("generation_age_seconds"),
            "average_open_age_seconds": average("open_age_seconds"),
            "average_decision_delay_seconds": average("decision_delay_seconds"),
            "age_bucket_policy_version": cls.attribution_policy_version,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
        }


DEFAULT_SIGNAL_AGE_AUTHORITY = SignalAgeAuthority()
