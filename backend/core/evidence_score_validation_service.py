"""Empirical validation authority for the live heuristic Evidence Score.

The production selector is a hand-authored score.  This service makes that
truth explicit, records every finalized candidate (not only promoted trades),
settles point-in-time counterfactual outcomes, and runs the existing purged
walk-forward authority against the selector itself.

No report from this service may authorize capital.  Until candidate-population
outcomes, benchmark coverage and fold requirements are satisfied, the selector
state remains ``UNVALIDATED_HEURISTIC`` even when software tests are green.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import statistics
import threading
from typing import Any, Dict, Iterable, Mapping, Optional

from core.evidence_engine_service import model_version_for_mode
from core.india_cost_model import IndiaCashCostModel
from core.production_mode_policy import POLICY_VERSION, policy_for, require_production_mode
from core.walk_forward_validation_service import WalkForwardValidationService
from core.ml_history_policy import policy_for_mode

VALIDATION_VERSION = "evidence-score-validation-1.2.0"
MIN_RESEARCH_SAMPLES = 100
MIN_RESEARCH_DAYS = 60
MIN_CAPITAL_PROFILE_SAMPLES = 300
MIN_CAPITAL_PROFILE_DAYS = 126
OUTCOME_HORIZON_BARS = {"intraday": 12, "delivery": 10}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "—"):
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _stamp(value: Any) -> Optional[datetime]:
    if value in (None, "", "—"):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(text[:10]).replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _candle_stamp(candle: Mapping[str, Any]) -> Optional[datetime]:
    return _stamp(candle.get("timestamp") or candle.get("time") or candle.get("date"))


def _rank(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    out = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + end - 1) / 2.0 + 1.0
        for position in range(index, end):
            out[ordered[position][0]] = rank
        index = end
    return out


def _corr(left: list[float], right: list[float]) -> Optional[float]:
    if len(left) != len(right) or len(left) < 3:
        return None
    mean_l, mean_r = statistics.fmean(left), statistics.fmean(right)
    dl = [value - mean_l for value in left]
    dr = [value - mean_r for value in right]
    denominator = math.sqrt(sum(value * value for value in dl) * sum(value * value for value in dr))
    if denominator <= 0:
        return None
    return sum(a * b for a, b in zip(dl, dr)) / denominator


def _policy_fingerprint(mode: str) -> str:
    payload = policy_for(mode).to_dict()
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


class EvidenceScoreValidationService:
    """Record and validate the actual score used to select candidates."""

    def __init__(self, store: Any):
        self.store = store
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS evidence_score_observations (
              observation_id TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              mode TEXT NOT NULL,
              side TEXT,
              observed_at TEXT NOT NULL,
              evidence_score REAL NOT NULL,
              evidence_readiness TEXT,
              evidence_scoring_state TEXT,
              promotion_threshold REAL,
              watch_threshold REAL,
              component_json TEXT NOT NULL,
              entry REAL,
              stop REAL,
              target REAL,
              initial_risk REAL,
              final_score REAL,
              final_status TEXT,
              final_decision TEXT,
              sector TEXT,
              regime TEXT,
              policy_version TEXT NOT NULL,
              policy_fingerprint TEXT NOT NULL,
              evidence_model_id TEXT NOT NULL,
              ranking_version TEXT,
              feature_json TEXT NOT NULL,
              outcome_status TEXT NOT NULL DEFAULT 'PENDING',
              outcome_price REAL,
              outcome_at TEXT,
              gross_return REAL,
              r_multiple REAL,
              cost_return REAL,
              benchmark_symbol TEXT NOT NULL DEFAULT 'NIFTY 50',
              benchmark_entry REAL,
              benchmark_outcome REAL,
              benchmark_return REAL,
              outcome_source TEXT,
              same_bar_ambiguous INTEGER NOT NULL DEFAULT 0,
              optimistic_outcome_status TEXT,
              censored_outcome_status TEXT,
              outcome_policy TEXT,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_evidence_score_pending_symbol
              ON evidence_score_observations(outcome_status, symbol, observed_at);
            CREATE INDEX IF NOT EXISTS ix_evidence_score_mode_time
              ON evidence_score_observations(mode, observed_at, outcome_status);
            CREATE TABLE IF NOT EXISTS evidence_score_validation_reports (
              report_id TEXT PRIMARY KEY,
              mode TEXT NOT NULL,
              policy_fingerprint TEXT NOT NULL,
              state TEXT NOT NULL,
              validated_at TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_evidence_score_report_mode_time
              ON evidence_score_validation_reports(mode, validated_at);
            """)
            columns = {str(row[1]) for row in self.store.conn.execute("PRAGMA table_info(evidence_score_observations)").fetchall()}
            for name, decl in (
                ("same_bar_ambiguous", "INTEGER NOT NULL DEFAULT 0"),
                ("optimistic_outcome_status", "TEXT"),
                ("censored_outcome_status", "TEXT"),
                ("outcome_policy", "TEXT"),
            ):
                if name not in columns:
                    self.store.conn.execute(f"ALTER TABLE evidence_score_observations ADD COLUMN {name} {decl}")
            self.store.conn.commit()

    def _evaluated_policy_trial_count(self, mode: str) -> int:
        """Count distinct selector-policy fingerprints actually evaluated.

        The DSR trial count must describe real policy variants, not the size of
        the unrelated factor zoo.  The current fingerprint is included even
        before its first persisted report.
        """
        fingerprints = {_policy_fingerprint(mode)}
        for table in ("evidence_score_observations", "evidence_score_validation_reports"):
            try:
                rows = self.store.conn.execute(
                    f"SELECT DISTINCT policy_fingerprint FROM {table} WHERE mode=?",
                    (mode,),
                ).fetchall()
                fingerprints.update(str(row[0]) for row in rows if row and row[0])
            except Exception:
                continue
        return max(1, len(fingerprints))

    def _latest_benchmark_price(self) -> Optional[float]:
        try:
            row = self.store.conn.execute(
                """SELECT ltp FROM quotes
                   WHERE instrument_key='NSE_INDEX|Nifty 50'
                     AND UPPER(COALESCE(exchange,'')) IN ('NSE_INDEX','NSE')
                     AND ltp IS NOT NULL AND ltp>1000
                   ORDER BY timestamp DESC LIMIT 1"""
            ).fetchone()
            return _num(row[0]) if row else None
        except Exception:
            return None

    @staticmethod
    def _observation_id(candidate: Mapping[str, Any], observed_at: str, evidence_model_id: str) -> str:
        material = "|".join(str(value or "") for value in (
            candidate.get("symbol"), candidate.get("mode"), candidate.get("side"),
            observed_at, candidate.get("entry") or candidate.get("planned_entry"),
            candidate.get("sl") or candidate.get("stop") or candidate.get("planned_sl"),
            candidate.get("t1") or candidate.get("target") or candidate.get("planned_t1"),
            evidence_model_id, candidate.get("ranking_version"),
        ))
        return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:32]

    def record(self, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        mode = require_production_mode(candidate.get("mode"))
        symbol = str(candidate.get("symbol") or "").upper().strip()
        evidence_score = _num(candidate.get("evidence_score"))
        if not symbol or evidence_score is None:
            return {"ok": False, "state": "SKIPPED_MISSING_EVIDENCE_IDENTITY"}
        policy = policy_for(mode)
        observed_at = str(candidate.get("decision_as_of") or candidate.get("last_ai_validation") or _now())
        evidence_model_id = str(candidate.get("evidence_model_id") or model_version_for_mode(mode))
        observation_id = self._observation_id(candidate, observed_at, evidence_model_id)
        entry = _num(candidate.get("entry") if candidate.get("entry") is not None else candidate.get("planned_entry"))
        stop = _num(candidate.get("sl") or candidate.get("stop") or candidate.get("planned_sl"))
        target = _num(candidate.get("t1") or candidate.get("target") or candidate.get("planned_t1"))
        initial_risk = abs(entry - stop) if entry is not None and stop is not None else None
        components = candidate.get("rank_components") or []
        feature_keys = (
            "rank_raw_score", "rank_effective_max_score", "rank_normalized_score",
            "rank_degraded_components", "rank_missing_inputs", "rank_gate_failures",
            "rank_veto_reasons", "source_engine_score", "index_context", "market_structure",
            "sector", "sector_label", "spread_bps", "rr", "freshness_state",
            "candle_freshness_state", "quote_age_seconds", "candle_age_seconds",
            "feature_as_of", "fundamental_as_of", "universe_as_of", "universe_id",
            "dataset_fingerprint", "feature_manifest_hash", "corporate_action_adjusted",
            "survivorship_bias_controlled", "admission_policy_version",
        )
        features = {key: candidate.get(key) for key in feature_keys if candidate.get(key) is not None}
        benchmark_entry = self._latest_benchmark_price()
        with self.store.write_lock:
            cursor = self.store.conn.execute(
                """INSERT OR IGNORE INTO evidence_score_observations(
                    observation_id,symbol,mode,side,observed_at,evidence_score,
                    evidence_readiness,evidence_scoring_state,promotion_threshold,watch_threshold,
                    component_json,entry,stop,target,initial_risk,final_score,final_status,
                    final_decision,sector,regime,policy_version,policy_fingerprint,
                    evidence_model_id,ranking_version,feature_json,benchmark_entry
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    observation_id, symbol, mode, str(candidate.get("side") or "").upper(), observed_at,
                    evidence_score, candidate.get("rank_readiness"), candidate.get("rank_scoring_state"),
                    float(policy.promotion_threshold), float(policy.watch_threshold),
                    json.dumps(components, sort_keys=True, default=str), entry, stop, target, initial_risk,
                    _num(candidate.get("rank_score") if candidate.get("rank_score") is not None else candidate.get("score")),
                    str(candidate.get("status") or ""), str(candidate.get("decision") or ""),
                    str(candidate.get("sector") or candidate.get("sector_label") or ""),
                    str(candidate.get("index_context") or ""),
                    str(candidate.get("policy_version") or POLICY_VERSION), _policy_fingerprint(mode),
                    evidence_model_id, str(candidate.get("ranking_version") or ""),
                    json.dumps(features, sort_keys=True, default=str), benchmark_entry,
                ),
            )
            self.store.conn.commit()
        return {
            "ok": True,
            "state": "RECORDED" if int(cursor.rowcount or 0) else "DUPLICATE",
            "observation_id": observation_id,
            "selector_state": "UNVALIDATED_HEURISTIC",
        }

    @staticmethod
    def _settled_values(row: Mapping[str, Any], price: float) -> tuple[Optional[float], Optional[float], Optional[float]]:
        entry = _num(row.get("entry"))
        stop = _num(row.get("stop"))
        side = str(row.get("side") or "LONG").upper()
        if entry is None or entry <= 0:
            return None, None, None
        pnl_points = entry - price if side == "SHORT" else price - entry
        gross_return = pnl_points / entry
        initial_risk = _num(row.get("initial_risk"))
        if initial_risk is None and stop is not None:
            initial_risk = abs(entry - stop)
        r_multiple = pnl_points / initial_risk if initial_risk and initial_risk > 0 else None
        model = IndiaCashCostModel.for_evidence(str(row.get("mode") or "delivery"), dict(row))
        if side == "SHORT":
            costs = model.round_trip(price, entry, 1)
        else:
            costs = model.round_trip(entry, price, 1)
        total_cost = _num((costs.get("costs") or {}).get("total")) or 0.0
        cost_return = total_cost / entry
        return gross_return, r_multiple, cost_return

    def mark_quotes(self, quotes: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
        clean = {
            str(symbol).upper(): _num(row.get("ltp"))
            for symbol, row in (quotes or {}).items()
            if isinstance(row, Mapping)
            and row.get("identity_verified") is True
            and row.get("stale") is not True
            and row.get("usable_for_promotion") is not False
            and str(row.get("freshness_state") or "").lower() in {"live", "closed_market"}
        }
        clean = {symbol: price for symbol, price in clean.items() if price is not None and price > 0}
        if not clean:
            return {"ok": True, "updated": 0}
        marks = ",".join("?" for _ in clean)
        rows = self.store.conn.execute(
            f"SELECT * FROM evidence_score_observations WHERE outcome_status='PENDING' AND symbol IN ({marks})",
            tuple(clean.keys()),
        ).fetchall()
        benchmark_outcome = self._latest_benchmark_price()
        updated = 0
        with self.store.write_lock:
            for raw in rows:
                row = dict(raw)
                price = clean.get(str(row.get("symbol") or "").upper())
                entry, stop, target = _num(row.get("entry")), _num(row.get("stop")), _num(row.get("target"))
                if price is None or entry is None or stop is None or target is None:
                    continue
                side = str(row.get("side") or "LONG").upper()
                target_hit = price <= target if side == "SHORT" else price >= target
                stop_hit = price >= stop if side == "SHORT" else price <= stop
                if target_hit and stop_hit:
                    outcome_status = "AMBIGUOUS"
                elif target_hit:
                    outcome_status = "TARGET"
                elif stop_hit:
                    outcome_status = "STOP"
                else:
                    continue
                gross_return, r_multiple, cost_return = self._settled_values(row, price)
                benchmark_entry = _num(row.get("benchmark_entry"))
                benchmark_return = (
                    benchmark_outcome / benchmark_entry - 1.0
                    if benchmark_outcome is not None and benchmark_entry is not None and benchmark_entry > 0
                    else None
                )
                self.store.conn.execute(
                    """UPDATE evidence_score_observations
                       SET outcome_status=?,outcome_price=?,outcome_at=?,gross_return=?,r_multiple=?,
                           cost_return=?,benchmark_outcome=?,benchmark_return=?,outcome_source='verified_quote'
                       WHERE observation_id=? AND outcome_status='PENDING'""",
                    (
                        outcome_status, price, _now(), gross_return, r_multiple, cost_return,
                        benchmark_outcome, benchmark_return, row.get("observation_id"),
                    ),
                )
                updated += 1
            if updated:
                self.store.conn.commit()
        return {"ok": True, "updated": updated}

    def mark_candles(self, symbol: str, mode: str, candles: Iterable[Mapping[str, Any]], *, identity_verified: bool) -> Dict[str, Any]:
        """Settle pending selector observations using post-decision candle paths.

        Target/stop ordering is resolved by the first later candle. If both are
        touched inside one candle, the primary validation result is STOP-first;
        an optimistic TARGET-first sensitivity and a censored AMBIGUOUS view are
        stored alongside it. If neither is touched by the governed horizon, the
        horizon close is recorded. This prevents validation from learning only
        from observations that eventually hit a target or stop.
        """
        mode = require_production_mode(mode)
        symbol = str(symbol or "").upper().strip()
        if not symbol or identity_verified is not True:
            return {"ok": False, "updated": 0, "state": "REJECTED_UNVERIFIED_IDENTITY"}
        clean = []
        for raw in candles or []:
            if not isinstance(raw, Mapping):
                continue
            stamp = _candle_stamp(raw)
            high, low, close = _num(raw.get("high")), _num(raw.get("low")), _num(raw.get("close"))
            if stamp is None or high is None or low is None or close is None or min(high, low, close) <= 0:
                continue
            clean.append((stamp, high, low, close))
        clean.sort(key=lambda item: item[0])
        if not clean:
            return {"ok": True, "updated": 0, "state": "NO_VALID_CANDLES"}
        rows = self.store.conn.execute(
            "SELECT * FROM evidence_score_observations WHERE outcome_status='PENDING' AND symbol=? AND mode=? ORDER BY observed_at",
            (symbol, mode),
        ).fetchall()
        horizon = int(OUTCOME_HORIZON_BARS[mode])
        benchmark_outcome = self._latest_benchmark_price()
        updated = ambiguous = horizon_settled = 0
        with self.store.write_lock:
            for raw in rows:
                row = dict(raw)
                observed = _stamp(row.get("observed_at"))
                entry, stop, target = _num(row.get("entry")), _num(row.get("stop")), _num(row.get("target"))
                if observed is None or entry is None or stop is None or target is None:
                    continue
                future = [item for item in clean if item[0] > observed]
                if not future:
                    continue
                # Intraday observations expire at the end of their decision
                # session even when fewer than 12 bars remain. Delivery uses
                # the next ten completed daily bars.
                if mode == "intraday":
                    same_session = [item for item in future if item[0].date() == observed.date()]
                    path = same_session[:horizon]
                    horizon_complete = bool(path) and (
                        len(path) >= horizon or path[-1][0].hour > 15 or (path[-1][0].hour == 15 and path[-1][0].minute >= 25)
                    )
                else:
                    path = future[:horizon]
                    horizon_complete = len(path) >= horizon
                if not path:
                    continue
                side = str(row.get("side") or "LONG").upper()
                outcome_status = None
                outcome_price = None
                outcome_stamp = None
                same_bar_ambiguous = 0
                optimistic_outcome_status = None
                censored_outcome_status = None
                outcome_policy = "FIRST_TOUCH"
                for stamp, high, low, close in path:
                    target_hit = low <= target if side == "SHORT" else high >= target
                    stop_hit = high >= stop if side == "SHORT" else low <= stop
                    if target_hit and stop_hit:
                        # Conservative primary result required by the contract:
                        # assume the stop was reached first. Preserve the other
                        # interpretations as explicit sensitivity metadata.
                        outcome_status, outcome_price, outcome_stamp = "STOP", stop, stamp
                        same_bar_ambiguous = 1
                        optimistic_outcome_status = "TARGET"
                        censored_outcome_status = "AMBIGUOUS"
                        outcome_policy = "SAME_BAR_STOP_FIRST_PRIMARY"
                        ambiguous += 1
                        break
                    if target_hit:
                        outcome_status, outcome_price, outcome_stamp = "TARGET", target, stamp
                        break
                    if stop_hit:
                        outcome_status, outcome_price, outcome_stamp = "STOP", stop, stamp
                        break
                if outcome_status is None and horizon_complete:
                    outcome_status, outcome_price, outcome_stamp = "HORIZON", path[-1][3], path[-1][0]
                    horizon_settled += 1
                if outcome_status is None or outcome_price is None or outcome_stamp is None:
                    continue
                gross_return, r_multiple, cost_return = self._settled_values(row, outcome_price)
                benchmark_entry = _num(row.get("benchmark_entry"))
                # A current benchmark quote is aligned enough for real-time
                # target/stop settlement. Historical horizon rows deliberately
                # leave the benchmark blank unless an aligned benchmark price
                # is available; incomplete baseline coverage then blocks approval.
                aligned_benchmark = benchmark_outcome if outcome_stamp >= clean[-1][0] else None
                benchmark_return = (
                    aligned_benchmark / benchmark_entry - 1.0
                    if aligned_benchmark is not None and benchmark_entry is not None and benchmark_entry > 0
                    else None
                )
                self.store.conn.execute(
                    """UPDATE evidence_score_observations
                       SET outcome_status=?,outcome_price=?,outcome_at=?,gross_return=?,r_multiple=?,
                           cost_return=?,benchmark_outcome=?,benchmark_return=?,
                           outcome_source=?,same_bar_ambiguous=?,optimistic_outcome_status=?,
                           censored_outcome_status=?,outcome_policy=?
                       WHERE observation_id=? AND outcome_status='PENDING'""",
                    (
                        outcome_status, outcome_price, outcome_stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                        gross_return, r_multiple, cost_return, aligned_benchmark, benchmark_return,
                        "verified_candle_path_stop_first" if same_bar_ambiguous else "verified_candle_path",
                        same_bar_ambiguous, optimistic_outcome_status, censored_outcome_status, outcome_policy,
                        row.get("observation_id"),
                    ),
                )
                updated += 1
            if updated:
                self.store.conn.commit()
        return {
            "ok": True, "updated": updated, "ambiguous": ambiguous,
            "same_bar_primary_policy": "STOP_FIRST",
            "same_bar_sensitivities": ["TARGET_FIRST_OPTIMISTIC", "CENSORED_AMBIGUOUS"],
            "horizon_settled": horizon_settled, "horizon_bars": horizon,
        }

    @staticmethod
    def _descriptive(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
        usable = []
        for row in rows:
            score = _num(row.get("evidence_score") if row.get("evidence_score") is not None else (row.get("rank_score") if row.get("rank_score") is not None else row.get("score")))
            gross = _num(row.get("gross_return") if row.get("gross_return") is not None else row.get("forward_return"))
            cost = _num(row.get("cost_return")) or 0.0
            if score is None or gross is None:
                continue
            usable.append((score, gross - cost))
        if not usable:
            return {
                "samples": 0, "win_rate": None, "mean_net_return": None,
                "pearson_score_return": None, "spearman_score_return": None,
                "score_bins": [],
            }
        scores = [item[0] for item in usable]
        returns = [item[1] for item in usable]
        ordered = sorted(usable, key=lambda item: item[0])
        bin_count = min(5, max(1, len(ordered) // 20))
        bins = []
        for index in range(bin_count):
            start = index * len(ordered) // bin_count
            end = (index + 1) * len(ordered) // bin_count
            chunk = ordered[start:end]
            if not chunk:
                continue
            bins.append({
                "bin": index + 1,
                "n": len(chunk),
                "mean_score": round(statistics.fmean(value[0] for value in chunk), 4),
                "mean_net_return": round(statistics.fmean(value[1] for value in chunk), 8),
                "win_rate": round(sum(value[1] > 0 for value in chunk) / len(chunk), 6),
            })
        return {
            "samples": len(usable),
            "win_rate": sum(value > 0 for value in returns) / len(returns),
            "mean_net_return": statistics.fmean(returns),
            "median_net_return": statistics.median(returns),
            "pearson_score_return": _corr(scores, returns),
            "spearman_score_return": _corr(_rank(scores), _rank(returns)),
            "score_bins": bins,
        }

    def _candidate_rows(self, mode: str) -> list[Dict[str, Any]]:
        mode = require_production_mode(mode)
        rows = self.store.conn.execute(
            """SELECT * FROM evidence_score_observations
               WHERE mode=? AND outcome_status IN ('TARGET','STOP','HORIZON')
                 AND gross_return IS NOT NULL
               ORDER BY observed_at, symbol""",
            (mode,),
        ).fetchall()
        prepared = []
        raw_rows = [dict(row) for row in rows]
        by_day: Dict[str, list[Dict[str, Any]]] = {}
        for row in raw_rows:
            by_day.setdefault(str(row.get("observed_at") or "")[:10], []).append(row)
        for row in raw_rows:
            day = str(row.get("observed_at") or "")[:10]
            peers = by_day.get(day) or []
            other_peers = [peer for peer in peers if peer.get("observation_id") != row.get("observation_id")]
            net_values = [
                (_num(peer.get("gross_return")) or 0.0) - (_num(peer.get("cost_return")) or 0.0)
                for peer in other_peers if _num(peer.get("gross_return")) is not None
            ]
            sector = str(row.get("sector") or "")
            sector_values = [
                (_num(peer.get("gross_return")) or 0.0) - (_num(peer.get("cost_return")) or 0.0)
                for peer in other_peers
                if str(peer.get("sector") or "") == sector and _num(peer.get("gross_return")) is not None
            ]
            baseline_returns = {"cash": 0.0}
            if net_values:
                baseline_returns["candidate_universe_equal_weight"] = statistics.fmean(net_values)
                seed = int(hashlib.sha256(str(row.get("observation_id") or "").encode()).hexdigest()[:8], 16)
                baseline_returns["deterministic_random_candidate"] = net_values[seed % len(net_values)]
            if sector_values:
                baseline_returns["sector_peer_equal_weight"] = statistics.fmean(sector_values)
            benchmark = _num(row.get("benchmark_return"))
            if benchmark is not None:
                baseline_returns["nifty_50"] = benchmark
            try:
                features = json.loads(row.get("feature_json") or "{}")
            except Exception:
                features = {}
            prepared.append({
                "symbol": row.get("symbol"),
                "mode": mode,
                "date": day,
                "decision_as_of": row.get("observed_at"),
                "outcome_as_of": row.get("outcome_at"),
                "rank_score": row.get("evidence_score"),
                "forward_return": row.get("gross_return"),
                "cost_return": row.get("cost_return"),
                "benchmark_return": benchmark,
                "baseline_returns": baseline_returns,
                "dataset_fingerprint": features.get("dataset_fingerprint"),
                "feature_manifest_hash": features.get("feature_manifest_hash"),
                "universe_id": features.get("universe_id"),
                "cost_model_version": IndiaCashCostModel.for_evidence(mode, features).config.version,
                "cost_model_profile": IndiaCashCostModel.for_evidence(mode, features).config.profile,
                "execution_model_version": row.get("outcome_source"),
                "admission_policy_version": features.get("admission_policy_version"),
                "feature_as_of": features.get("feature_as_of") or row.get("observed_at"),
                "fundamental_as_of": features.get("fundamental_as_of"),
                "universe_as_of": features.get("universe_as_of"),
                "corporate_action_adjusted": features.get("corporate_action_adjusted") is True,
                "survivorship_bias_controlled": features.get("survivorship_bias_controlled") is True,
            })
        return prepared

    def _promoted_only_rows(self, mode: str) -> list[Dict[str, Any]]:
        mode = require_production_mode(mode)
        try:
            rows = self.store.conn.execute(
                """SELECT symbol,exchange,mode,side,score,entry,exit,pnl_points,opened_at,closed_at,status,payload_json
                   FROM signal_ledger
                   WHERE mode=? AND pnl_points IS NOT NULL
                     AND status IN ('SUCCESS','FAIL','EXPIRED','AMBIGUOUS')
                   ORDER BY opened_at,symbol""",
                (mode,),
            ).fetchall()
        except Exception:
            return []
        prepared = []
        for raw in rows:
            row = dict(raw)
            try:
                payload = json.loads(row.get("payload_json") or "{}")
                payload = dict(payload) if isinstance(payload, dict) else {}
            except Exception:
                payload = {}
            try:
                model = IndiaCashCostModel.for_evidence(mode, {**payload, **row})
            except ValueError:
                # Historical BSE rows without their scrip-group evidence cannot
                # be assigned a trustworthy net return.  Exclude rather than
                # silently applying NSE fees.
                continue
            entry = _num(row.get("entry"))
            pnl = _num(row.get("pnl_points"))
            if entry is None or entry <= 0 or pnl is None:
                continue
            exit_price = _num(row.get("exit"))
            side = str(row.get("side") or "LONG").upper()
            if exit_price is None:
                exit_price = entry - pnl if side == "SHORT" else entry + pnl
            costs = model.round_trip(exit_price, entry, 1) if side == "SHORT" else model.round_trip(entry, exit_price, 1)
            cost_return = (_num((costs.get("costs") or {}).get("total")) or 0.0) / entry
            prepared.append({
                "symbol": row.get("symbol"), "mode": mode,
                "date": str(row.get("opened_at") or "")[:10],
                "decision_as_of": row.get("opened_at"), "outcome_as_of": row.get("closed_at"),
                "rank_score": row.get("score"), "score": row.get("score"),
                "forward_return": pnl / entry, "cost_return": cost_return,
                "benchmark_return": None, "baseline_returns": {"cash": 0.0},
            })
        return prepared

    def validate(self, mode: str, *, persist: bool = True) -> Dict[str, Any]:
        mode = require_production_mode(mode)
        rows = self._candidate_rows(mode)
        descriptive = self._descriptive(rows)
        unique_days = len({str(row.get("date") or "")[:10] for row in rows if row.get("date")})
        regimes = sorted({
            str(raw[0] or "").strip().lower()
            for raw in self.store.conn.execute(
                """SELECT DISTINCT regime FROM evidence_score_observations
                   WHERE mode=? AND outcome_status IN ('TARGET','STOP','HORIZON')
                     AND regime IS NOT NULL AND TRIM(regime)<>''""",
                (mode,),
            ).fetchall()
            if str(raw[0] or "").strip()
        })
        benchmark_coverage = (
            sum(row.get("benchmark_return") is not None for row in rows) / len(rows)
            if rows else 0.0
        )
        baseline_names = (
            "cash", "candidate_universe_equal_weight", "deterministic_random_candidate",
            "sector_peer_equal_weight", "nifty_50",
        )
        baseline_coverage = {
            name: sum(name in (row.get("baseline_returns") or {}) for row in rows) / len(rows)
            for name in baseline_names
        } if rows else {}
        research_ready = len(rows) >= MIN_RESEARCH_SAMPLES and unique_days >= MIN_RESEARCH_DAYS
        capital_sample_ready = len(rows) >= MIN_CAPITAL_PROFILE_SAMPLES and unique_days >= MIN_CAPITAL_PROFILE_DAYS
        horizon = 1 if mode == "intraday" else 10
        mode_history_policy = policy_for_mode(mode)
        research_min_train = int(mode_history_policy.per_symbol_minimum_days)
        research_test_days = 10 if mode == "intraday" else 42
        authority = WalkForwardValidationService(self.store)
        trial_count = self._evaluated_policy_trial_count(mode)
        research_validation = authority.validate(
            model_id=model_version_for_mode(mode) + ":heuristic-selector:research", observations=rows,
            horizon_days=horizon, min_train_days=research_min_train, test_days=research_test_days,
            purge_days=horizon, max_folds=8, min_samples=MIN_RESEARCH_SAMPLES,
            persist=False, profile="research", trial_count=trial_count, embargo_days=horizon,
        ) if rows else None
        capital_validation = authority.validate(
            model_id=model_version_for_mode(mode) + ":heuristic-selector:capital", observations=rows,
            horizon_days=horizon, min_train_days=int(mode_history_policy.minimum_days),
            test_days=(20 if mode == "intraday" else 63), purge_days=horizon,
            max_folds=8, min_samples=MIN_CAPITAL_PROFILE_SAMPLES,
            persist=False, profile="capital", trial_count=trial_count, embargo_days=horizon,
        ) if rows else None
        selector_gates = {
            "minimum_candidate_population_samples_300": len(rows) >= MIN_CAPITAL_PROFILE_SAMPLES,
            "minimum_observation_days_126": unique_days >= MIN_CAPITAL_PROFILE_DAYS,
            "minimum_three_observed_regimes": len(regimes) >= 3,
            "positive_score_return_spearman": (descriptive.get("spearman_score_return") or -1.0) > 0,
            "positive_post_cost_expectancy": (descriptive.get("mean_net_return") or 0.0) > 0,
            "benchmark_coverage_complete": benchmark_coverage == 1.0,
            "broad_equal_weight_baseline_complete": baseline_coverage.get("candidate_universe_equal_weight") == 1.0,
            "random_pick_baseline_complete": baseline_coverage.get("deterministic_random_candidate") == 1.0,
            "sector_matched_baseline_complete": baseline_coverage.get("sector_peer_equal_weight") == 1.0,
            "nifty_baseline_complete": baseline_coverage.get("nifty_50") == 1.0,
            "capital_walk_forward_gates_pass": bool(capital_validation and capital_validation.get("approved")),
        }
        ambiguity_row = self.store.conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN same_bar_ambiguous=1 THEN 1 ELSE 0 END) ambiguous
               FROM evidence_score_observations
               WHERE mode=? AND outcome_status IN ('TARGET','STOP','HORIZON')""",
            (mode,),
        ).fetchone()
        ambiguity_total = int(ambiguity_row[0] or 0) if ambiguity_row else 0
        ambiguity_count = int(ambiguity_row[1] or 0) if ambiguity_row else 0
        ambiguity_policy = {
            "primary": "STOP_FIRST",
            "censored_sensitivity": "AMBIGUOUS",
            "optimistic_sensitivity": "TARGET_FIRST",
            "same_bar_count": ambiguity_count,
            "settled_sample_count": ambiguity_total,
            "same_bar_rate": round(ambiguity_count / ambiguity_total, 6) if ambiguity_total else 0.0,
        }
        approved = capital_sample_ready and all(selector_gates.values())
        if approved:
            state = "APPROVED_FOR_SHADOW"
        elif not rows:
            state = "NOT_RUN"
        elif not research_ready:
            state = "COLLECTING_CANDIDATE_OUTCOMES"
        elif research_validation and research_validation.get("approved") and not capital_sample_ready:
            state = "RESEARCH_SIGNAL_ONLY_INSUFFICIENT_CAPITAL_EVIDENCE"
        elif benchmark_coverage < 1.0 or any(
            baseline_coverage.get(name, 0.0) < 1.0
            for name in ("candidate_universe_equal_weight", "deterministic_random_candidate", "sector_peer_equal_weight", "nifty_50")
        ):
            state = "BLOCKED_MISSING_BASELINES"
        elif len(regimes) < 3:
            state = "BLOCKED_INSUFFICIENT_REGIME_DIVERSITY"
        else:
            state = "REJECTED_OR_INCOMPLETE"
        result = {
            "ok": True,
            "validation_version": VALIDATION_VERSION,
            "mode": mode,
            "selector": "heuristic_evidence_score",
            "selector_state": state,
            "approved_for_shadow": approved,
            "capital_authority": "NONE",
            "policy_version": POLICY_VERSION,
            "policy_fingerprint": _policy_fingerprint(mode),
            "weights_source": "STATIC_DESIGN_TIME_CONSTANTS",
            "weight_change_policy": "Any weight or threshold change creates a new fingerprint and requires a completely new candidate-population validation.",
            "multiple_testing_control": {
                "trial_count": trial_count,
                "trial_count_source": "distinct evaluated selector-policy fingerprints",
                "active": trial_count > 1,
                "note": "DSR and adjusted p-values are unadjusted when only one selector policy has been evaluated." if trial_count == 1 else "DSR and adjusted p-values include every distinct evaluated selector-policy fingerprint.",
            },
            "candidate_population_samples": len(rows),
            "candidate_population_days": unique_days,
            "observed_regimes": regimes,
            "benchmark_coverage": benchmark_coverage,
            "baseline_coverage": baseline_coverage,
            "same_bar_outcome_policy": ambiguity_policy,
            "baseline_definitions": {
                "cash": "zero return",
                "candidate_universe_equal_weight": "leave-one-out equal-weight return of all other candidates observed that day",
                "deterministic_random_candidate": "one deterministic hash-selected peer candidate from the same day",
                "sector_peer_equal_weight": "leave-one-out equal-weight return of same-sector candidates observed that day",
                "nifty_50": "point-in-time NIFTY 50 return over the same outcome interval",
            },
            "descriptive": descriptive,
            "selector_gates": selector_gates,
            "research_walk_forward": research_validation,
            "capital_walk_forward": capital_validation,
            # Compatibility alias; capital profile is the only report that can
            # qualify the selector for shadow approval.
            "walk_forward": capital_validation,
            "validated_at": _now(),
            "limitations": [
                "No live-capital authority is granted.",
                "Promoted-only ledger history is selection-biased and is reported separately.",
                "Factor research remains excluded unless an independently approved AI model is present.",
                "Survivorship, corporate-action, point-in-time lineage, regime diversity and aligned baselines are mandatory capital-profile gates.",
                "A research-profile pass is diagnostic only and cannot label the selector production-validated.",
                "Same-bar target/stop collisions use stop-first as the primary result; target-first and censored views are sensitivity only.",
            ],
        }
        canonical = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
        report_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        result["report_id"] = report_id
        if persist:
            with self.store.write_lock:
                self.store.conn.execute(
                    """INSERT OR REPLACE INTO evidence_score_validation_reports(
                       report_id,mode,policy_fingerprint,state,validated_at,payload_json
                       ) VALUES(?,?,?,?,?,?)""",
                    (report_id, mode, result["policy_fingerprint"], state, result["validated_at"], json.dumps(result, sort_keys=True, default=str)),
                )
                self.store.conn.commit()
        return result

    def promoted_only_diagnostic(self, mode: str) -> Dict[str, Any]:
        mode = require_production_mode(mode)
        rows = self._promoted_only_rows(mode)
        horizon = 1 if mode == "intraday" else 10
        trial_count = self._evaluated_policy_trial_count(mode)
        walk_forward = WalkForwardValidationService(self.store).validate(
            model_id=model_version_for_mode(mode) + ":promoted-only-biased-diagnostic",
            observations=rows, horizon_days=horizon,
            min_train_days=(20 if mode == "intraday" else 63),
            test_days=(5 if mode == "intraday" else 21), purge_days=horizon,
            max_folds=8, min_samples=max(20, min(MIN_RESEARCH_SAMPLES, len(rows))),
            persist=False, profile="research", trial_count=trial_count, embargo_days=horizon,
        ) if rows else None
        return {
            "scope": "PROMOTED_ONLY_SELECTION_BIASED",
            "selector_approval_allowed": False,
            "samples": len(rows),
            "descriptive": self._descriptive(rows),
            "walk_forward_diagnostic": walk_forward,
            "reason": "signal_ledger contains only actionable/promoted signals; this diagnostic can describe realised promoted trades but cannot prove candidate-selection quality.",
        }

    def status(self) -> Dict[str, Any]:
        desks: Dict[str, Any] = {}
        for mode in ("intraday", "delivery"):
            latest = self.store.conn.execute(
                """SELECT payload_json FROM evidence_score_validation_reports
                   WHERE mode=? AND policy_fingerprint=? ORDER BY validated_at DESC LIMIT 1""",
                (mode, _policy_fingerprint(mode)),
            ).fetchone()
            pending = self.store.conn.execute(
                "SELECT COUNT(*) FROM evidence_score_observations WHERE mode=? AND outcome_status='PENDING'",
                (mode,),
            ).fetchone()[0]
            settled = self.store.conn.execute(
                """SELECT COUNT(*) FROM evidence_score_observations
                   WHERE mode=? AND outcome_status IN ('TARGET','STOP','HORIZON')""",
                (mode,),
            ).fetchone()[0]
            total = self.store.conn.execute(
                "SELECT COUNT(*) FROM evidence_score_observations WHERE mode=?",
                (mode,),
            ).fetchone()[0]
            persisted = json.loads(latest[0]) if latest else None
            persisted_samples = int((persisted or {}).get("candidate_population_samples") or 0)
            report_recomputed = persisted is None or persisted_samples != int(settled or 0)
            report = self.validate(mode, persist=False) if report_recomputed else persisted
            desks[mode] = {
                **report,
                "observations_total": int(total or 0),
                "observations_pending": int(pending or 0),
                "observations_settled": int(settled or 0),
                "report_recomputed_from_current_ledger": report_recomputed,
                "promoted_only_diagnostic": self.promoted_only_diagnostic(mode),
            }
        return {
            "ok": True,
            "validation_version": VALIDATION_VERSION,
            "overall_state": (
                "APPROVED_FOR_SHADOW" if all(row.get("approved_for_shadow") for row in desks.values())
                else "UNVALIDATED_HEURISTIC"
            ),
            "primary_selector": "evidence_score",
            "factor_platform_influence": "NONE_UNLESS_AI_MODEL_AND_WALK_FORWARD_APPROVAL_EXIST",
            "desks": desks,
            "policy": "Software tests do not validate trading edge. Candidate-population outcomes and baselines must pass before selection is described as validated.",
        }
