"""Canonical purge/embargo and point-in-time leakage proof for research validation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Sequence


class TemporalLeakageAuthority:
    authority = "TemporalLeakageAuthority"
    authority_version = "1.1.0-canary-proof"

    @staticmethod
    def _parse(value: Any) -> datetime | None:
        if value in (None, "", "—"):
            return None
        text = str(value).strip().replace("Z", "+00:00")
        try:
            stamp = datetime.fromisoformat(text)
            return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                return datetime.fromisoformat(text[:10]).replace(tzinfo=timezone.utc)
            except ValueError:
                return None

    @classmethod
    def validate_folds(cls, dates: Iterable[str], folds: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        unique = sorted({str(d)[:10] for d in dates if d})
        index = {day: i for i, day in enumerate(unique)}
        violations: list[str] = []
        prior_test_end_index: int | None = None
        seen_test_dates: set[str] = set()
        for fold in folds:
            train = [str(x)[:10] for x in (fold.get("train_dates") or [])]
            test = [str(x)[:10] for x in (fold.get("test_dates") or [])]
            number = int(fold.get("fold") or 0)
            if not train or not test:
                violations.append(f"fold {number}: train/test dates required")
                continue
            if set(train) & set(test):
                violations.append(f"fold {number}: train/test overlap")
            overlap = seen_test_dates & set(test)
            if overlap:
                violations.append(f"fold {number}: test dates reused")
            seen_test_dates.update(test)
            try:
                train_end = index[train[-1]]
                test_start = index[test[0]]
                test_end = index[test[-1]]
            except KeyError:
                violations.append(f"fold {number}: dates absent from canonical date index")
                continue
            if train_end >= test_start:
                violations.append(f"fold {number}: train is not strictly before test")
            actual_purge = test_start - train_end - 1
            required_purge = max(0, int(fold.get("purge_days") or 0))
            if actual_purge < required_purge:
                violations.append(f"fold {number}: purge gap {actual_purge} < {required_purge}")
            required_embargo = max(0, int(fold.get("embargo_days") or 0))
            if prior_test_end_index is not None:
                actual_embargo = test_start - prior_test_end_index - 1
                if actual_embargo < required_embargo:
                    violations.append(f"fold {number}: embargo gap {actual_embargo} < {required_embargo}")
            prior_test_end_index = test_end
        return {
            "ok": not violations,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "fold_count": len(folds),
            "violations": violations,
            "policy": "train strictly precedes test; purge precedes each test; declared embargo separates consecutive test windows; test dates never overlap",
        }

    @classmethod
    def validate_observation(cls, row: Mapping[str, Any]) -> Dict[str, Any]:
        decision = cls._parse(row.get("decision_as_of") or row.get("as_of"))
        outcome = cls._parse(row.get("outcome_as_of") or row.get("date"))
        mode = str(row.get("mode") or "").lower()
        fields = ["feature_as_of", "universe_as_of"]
        if mode == "delivery":
            fields.append("fundamental_as_of")
        stamps = {field: cls._parse(row.get(field)) for field in fields}
        blockers: list[str] = []
        if decision is None:
            blockers.append("DECISION_TIME_MISSING")
        if outcome is None:
            blockers.append("OUTCOME_TIME_MISSING")
        if decision is not None and outcome is not None and decision > outcome:
            blockers.append("DECISION_AFTER_OUTCOME")
        for field, stamp in stamps.items():
            if stamp is None:
                blockers.append(f"{field.upper()}_MISSING")
            elif decision is not None and stamp > decision:
                blockers.append(f"{field.upper()}_AFTER_DECISION")
        return {
            "ok": not blockers,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "blockers": blockers,
            "decision_as_of": decision.isoformat() if decision else None,
            "outcome_as_of": outcome.isoformat() if outcome else None,
            "required_feature_times": {key: value.isoformat() if value else None for key, value in stamps.items()},
            "policy": "features, universe and Delivery fundamentals must exist no later than decision time; decision must not occur after outcome evidence",
        }

    @classmethod
    def run_canary_suite(cls) -> Dict[str, Any]:
        """Prove the authority rejects deliberately contaminated temporal twins.

        This is an executable self-test of the production authority itself.  It
        does not claim to detect every possible research error; instead it
        continuously proves the concrete point-in-time and fold-boundary
        invariants that this authority is responsible for enforcing.
        """
        clean = {
            "mode": "delivery",
            "decision_as_of": "2026-01-05T10:00:00+00:00",
            "outcome_as_of": "2026-01-05T15:00:00+00:00",
            "feature_as_of": "2026-01-05T09:59:00+00:00",
            "universe_as_of": "2026-01-05T09:59:00+00:00",
            "fundamental_as_of": "2026-01-05T09:59:00+00:00",
        }
        observation_cases = {
            "clean_causal_observation": (clean, None),
            "feature_after_decision": ({**clean, "feature_as_of": "2026-01-05T10:01:00+00:00"}, "FEATURE_AS_OF_AFTER_DECISION"),
            "universe_after_decision": ({**clean, "universe_as_of": "2026-01-05T10:01:00+00:00"}, "UNIVERSE_AS_OF_AFTER_DECISION"),
            "fundamental_after_decision": ({**clean, "fundamental_as_of": "2026-01-05T10:01:00+00:00"}, "FUNDAMENTAL_AS_OF_AFTER_DECISION"),
            "decision_after_outcome": ({**clean, "decision_as_of": "2026-01-05T15:01:00+00:00"}, "DECISION_AFTER_OUTCOME"),
        }
        observation_results: Dict[str, Any] = {}
        observation_ok = True
        for name, (row, expected_blocker) in observation_cases.items():
            proof = cls.validate_observation(row)
            blockers = set(proof.get("blockers") or [])
            passed = proof.get("ok") is True if expected_blocker is None else (proof.get("ok") is False and expected_blocker in blockers)
            observation_ok = observation_ok and passed
            observation_results[name] = {
                "passed": passed,
                "expected_blocker": expected_blocker,
                "observed_blockers": sorted(blockers),
            }

        dates = [f"2026-01-{day:02d}" for day in range(1, 11)]
        clean_folds = [
            {"fold": 1, "train_dates": dates[0:3], "test_dates": dates[4:5], "purge_days": 1, "embargo_days": 0},
            {"fold": 2, "train_dates": dates[0:4], "test_dates": dates[6:7], "purge_days": 1, "embargo_days": 1},
        ]
        fold_cases = {
            "clean_purged_embargoed_folds": (clean_folds, None),
            "train_test_overlap": ([{
                "fold": 1, "train_dates": dates[0:5], "test_dates": dates[4:5],
                "purge_days": 0, "embargo_days": 0,
            }], "train/test overlap"),
            "purge_gap_removed": ([{
                "fold": 1, "train_dates": dates[0:4], "test_dates": dates[4:5],
                "purge_days": 1, "embargo_days": 0,
            }], "purge gap"),
            "embargo_gap_removed": ([
                {"fold": 1, "train_dates": dates[0:3], "test_dates": dates[4:5], "purge_days": 1, "embargo_days": 0},
                {"fold": 2, "train_dates": dates[0:4], "test_dates": dates[5:6], "purge_days": 1, "embargo_days": 1},
            ], "embargo gap"),
        }
        fold_results: Dict[str, Any] = {}
        fold_ok = True
        for name, (folds, expected_fragment) in fold_cases.items():
            proof = cls.validate_folds(dates, folds)
            violations = list(proof.get("violations") or [])
            if expected_fragment is None:
                passed = proof.get("ok") is True
            else:
                passed = proof.get("ok") is False and any(expected_fragment in item for item in violations)
            fold_ok = fold_ok and passed
            fold_results[name] = {
                "passed": passed,
                "expected_violation_fragment": expected_fragment,
                "observed_violations": violations,
            }

        return {
            "ok": observation_ok and fold_ok,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "observation_canaries": observation_results,
            "fold_canaries": fold_results,
            "policy": "clean twins must pass and deliberately contaminated point-in-time/purge/embargo twins must fail closed",
        }


DEFAULT_TEMPORAL_LEAKAGE_AUTHORITY = TemporalLeakageAuthority()
