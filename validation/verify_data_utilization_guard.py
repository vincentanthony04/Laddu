"""Deterministic proof that retained history participates before scanner deep gates.

No provider/database/broker access is used. This guard proves the pure
historical scheduling mathematics and source contracts that keep the R20 data
utilization path local-only, bounded and non-authoritative for trade promotion.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.historical_alpha_scheduling_service import HistoricalAlphaSchedulingService


def _trend_rows(count: int = 800, daily: float = 0.0007, volume: float = 100_000.0):
    rows = []
    price = 100.0
    for i in range(count):
        price *= 1.0 + daily
        rows.append({
            "timestamp": f"2020-01-{(i % 28) + 1:02d}T00:00:00Z",
            "close": price,
            "volume": volume * (1.0 + (i % 20) / 100.0),
        })
    return rows


def _flat_rows(count: int = 800):
    rows = []
    for i in range(count):
        rows.append({
            "timestamp": f"2020-01-{(i % 28) + 1:02d}T00:00:00Z",
            "close": 100.0 + ((i % 5) - 2) * 0.01,
            "volume": 50_000.0,
        })
    return rows


def main() -> int:
    checks = {}
    strong = HistoricalAlphaSchedulingService._score_rows(_trend_rows())
    flat = HistoricalAlphaSchedulingService._score_rows(_flat_rows())
    checks["deep_history_consumed"] = int(strong.get("depth") or 0) >= 756
    checks["long_horizon_evidence_present"] = strong.get("returns", {}).get("504") is not None and strong.get("returns", {}).get("756") is not None
    checks["delivery_history_changes_scheduling"] = float(strong.get("delivery_score") or 0) > float(flat.get("delivery_score") or 0)
    checks["scheduling_never_trade_confidence"] = strong.get("trade_confidence_affected") is False and flat.get("trade_confidence_affected") is False

    adapter = (ROOT / "backend" / "research_adapter.py").read_text(encoding="utf-8-sig")
    scanner = (ROOT / "backend" / "core" / "scan_orchestration_modes.py").read_text(encoding="utf-8-sig")
    trainer = (ROOT / "backend" / "tools" / "train_nse_smart_model.py").read_text(encoding="utf-8-sig")
    panel = (ROOT / "backend" / "core" / "factors" / "universe_panel_service.py").read_text(encoding="utf-8-sig")

    checks["research_tail_deepened"] = 'candle_tail = 600 if str(mode or "").lower() == "intraday" else 756' in adapter
    checks["delivery_history_deepened"] = 'get_delivery_data(symbol, days=504)' in adapter
    checks["cross_sectional_cache_merges_batches"] = 'self._cs_scores.update(scores)' in adapter and 'self._cs_symbol_refreshed_at' in adapter
    checks["cross_sectional_panel_400_bars"] = 'limit: int = 400' in panel
    checks["scanner_uses_history_before_deep_gate"] = scanner.find('historical_alpha_scheduling.refresh_async(valid_batch)') < scanner.find('ranked = sorted(quote_ready, key=delivery_quote_rank, reverse=True)')
    checks["intraday_history_in_scheduling_rank"] = 'historical_alpha_scheduling.score_for(symbol, mode)' in scanner
    checks["wfa_all_eligible_folds"] = 'oof_test_dates, oof_max_folds = 63, 0  # 0 = every eligible fold' in trainer
    checks["wfa_multi_year_initial_history"] = ('ML_HISTORICAL_TRAIN_TARGET_DAYS' in trainer and 'oof_start_dates = adaptive_train_dates if first_mode else adaptive_train_dates + int(horizon)' in trainer)
    checks["wfa_expanding_adaptive_history"] = ('adaptive_history_mode=(None if first_mode else "delivery")' in trainer and 'training_frame_and_weights(tr, mode=str(adaptive_history_mode))' in trainer and 'int(training_policy.get("maximum_days") or 0) <= 0' in trainer and 'train_window_days=(None if first_mode or' in trainer)
    checks["final_model_uses_governed_window"] = ('training_frame_and_weights(' in trainer and 'final_training_source = labelled' in trainer and 'final_model.fit(final_training[FEATURES]' in trainer)

    failed = [name for name, ok in checks.items() if ok is not True]
    report = {
        "ok": not failed,
        "version": "data-utilization-alpha-funnel-1.0.0",
        "checks": checks,
        "passed": len(checks) - len(failed),
        "total": len(checks),
        "failed": failed,
        "broker_authority": "NONE",
        "policy": "retained history may schedule scarce analysis earlier; it cannot create trade conviction or bypass canonical evidence/risk gates",
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
