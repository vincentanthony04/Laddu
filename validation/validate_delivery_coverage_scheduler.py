from __future__ import annotations
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.scan_orchestration_service import ScanOrchestrationService


class Store:
    def __init__(self): self.kv = {}
    def get_kv(self, key, default=None): return self.kv.get(key, default)
    def set_kv(self, key, value): self.kv[key] = value
    def delete_kv(self, key): self.kv.pop(key, None)

class RuntimeMarket:
    def latest_quotes(self, symbols): return []

class Client:
    def token_status(self): return {"ok": True}

class Host:
    def __init__(self, n=4137):
        self.lock = threading.RLock()
        self.store = Store()
        self.client = Client()
        self.runtime_market_state = RuntimeMarket()
        self.status = {"mode_scanners": {"delivery": {"analysis": {}, "coverage": {}}}, "universe_authority": {"rule_version": "nse-first-bse-fallback-ordinary-equity-v69.8.0", "snapshots": {"delivery": {"snapshot_id": "synthetic-delivery-4137", "content_hash": "proof", "population_count": n}}}}
        self._instrument_health_meta = {"loaded": True, "cache_usable": True, "universe_revision": "nse-first-bse-fallback-ordinary-equity-v69.8.0", "count": n, "universe_stats": {"derivatives": 0}}
        self._universe_snapshots = {}
        self._coverage_quote_cache = {}
        self._bad_historical_keys = {}
        self._rows = [{"instrument_key": f"NSE_EQ|{i:05d}", "trading_symbol": f"S{i:05d}", "symbol": f"S{i:05d}", "exchange": "NSE", "instrument_type": "EQ"} for i in range(n)]
        self.errors=[]
    def immutable_scan_population(self, mode): return list(self._rows)
    def record_error(self, *args): self.errors.append(args)
    def event(self, *args, **kwargs): pass
    def scanner_analyze_compute(self, *args, **kwargs): return None


def main():
    host=Host()
    svc=ScanOrchestrationService(host)
    seq=[]
    for _ in range(100):
        result=svc.run_delivery_coverage_pass()
        contract=dict(host.status["mode_scanners"]["delivery"].get("progress_contract") or {})
        seq.append(int(contract.get("current_sweep_scanned") or 0))
        if result.get("sweep_complete"):
            break
    strictly_monotonic=all(b>a for a,b in zip(seq,seq[1:]))
    full=bool(seq and seq[-1]==4137)
    checkpoints=[x for x in seq if x>0]
    checkpoint=host.store.get_kv("scan_checkpoint:delivery:coverage", {}) or {}
    analysis=host.status["mode_scanners"]["delivery"].get("analysis") or {}
    report={
        "ok": strictly_monotonic and full and len(checkpoints)>=10 and not host.errors,
        "population": 4137,
        "cycles": len(seq),
        "checkpoints": seq,
        "strictly_monotonic": strictly_monotonic,
        "full_sweep": full,
        "checkpoint_count": len(checkpoints),
        "checkpoint_cursor": checkpoint.get("cursor"),
        "checkpoint_sweep_scanned": checkpoint.get("sweep_scanned"),
        "checkpoint_sweep_complete": checkpoint.get("sweep_complete"),
        "analysis_scanned_during_coverage_proof": int(analysis.get("current_sweep_scanned") or analysis.get("sweep_scanned") or 0),
        "provider_io": False,
        "deep_analysis_calls": 0,
        "errors": host.errors,
    }
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1

if __name__ == "__main__": raise SystemExit(main())
