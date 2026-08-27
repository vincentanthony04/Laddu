"""Durable, signal-specific 5/10/20 trading-day outcome ledger."""
from __future__ import annotations

import threading
import json
import statistics
from typing import Any, Dict, Iterable, List

HORIZONS = (5, 10, 20)


class InstitutionalOutcomeService:
    def __init__(self, store: Any):
        self.store = store
        # v60.14 P0 fix: write_lock may be absent on lightweight test doubles.
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.Lock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript("""
        CREATE TABLE IF NOT EXISTS institutional_signal_observations (
          observation_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, signal_date TEXT NOT NULL,
          model_version TEXT NOT NULL, score REAL NOT NULL, stage TEXT, entry_price REAL,
          hidden_accumulation INTEGER NOT NULL DEFAULT 0, volume_climax INTEGER NOT NULL DEFAULT 0,
          absorption INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(symbol,signal_date,model_version)
        );
        CREATE TABLE IF NOT EXISTS institutional_signal_outcomes (
          observation_id TEXT NOT NULL, horizon_days INTEGER NOT NULL, outcome_date TEXT,
          forward_return REAL, benchmark_return REAL, excess_return REAL, status TEXT NOT NULL,
          payload_json TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(observation_id,horizon_days)
        );
        CREATE INDEX IF NOT EXISTS ix_inst_signal_date ON institutional_signal_observations(signal_date,symbol);
        CREATE TABLE IF NOT EXISTS institutional_lifecycle_transitions (
          symbol TEXT NOT NULL, transition_date TEXT NOT NULL, from_stage TEXT, to_stage TEXT NOT NULL,
          observation_id TEXT NOT NULL, model_version TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(symbol,transition_date,model_version)
        );
        """)
            self.store.conn.commit()

    def record(self, result: Dict[str, Any]) -> Dict[str, Any]:
        if not result.get("ok") or not result.get("signal_date"):
            return {"ok": False, "state": result.get("state") or "not_ready"}
        symbol, day, version = result.get("symbol"), result.get("signal_date"), result.get("model_version")
        oid = f"{symbol}:{day}:{version}"
        sig = result.get("signals") or {}
        # v60.14 P0 fix: these multi-statement writes previously ran without
        # Store.write_lock -- routed through it now, same as the repos fixed
        # for "database is locked".
        with self.store.write_lock:
            self.store.conn.execute("""INSERT OR REPLACE INTO institutional_signal_observations
              (observation_id,symbol,signal_date,model_version,score,stage,entry_price,hidden_accumulation,volume_climax,absorption,payload_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (oid,symbol,day,version,float(result.get("score") or 0),result.get("stage"),result.get("price"),
              1 if sig.get("hidden_accumulation") else 0,1 if sig.get("volume_climax") else 0,1 if sig.get("absorption") else 0,json.dumps(result,sort_keys=True,default=str)))
            previous=self.store.conn.execute("SELECT stage FROM institutional_signal_observations WHERE symbol=? AND signal_date<? ORDER BY signal_date DESC LIMIT 1",(symbol,day)).fetchone()
            from_stage=previous[0] if previous else None
            if from_stage != result.get("stage"):
                self.store.conn.execute("INSERT OR REPLACE INTO institutional_lifecycle_transitions(symbol,transition_date,from_stage,to_stage,observation_id,model_version) VALUES(?,?,?,?,?,?)",
                                        (symbol,day,from_stage,result.get("stage"),oid,version))
            for h in HORIZONS:
                self.store.conn.execute("INSERT OR IGNORE INTO institutional_signal_outcomes(observation_id,horizon_days,status,payload_json) VALUES(?,?,?,?)",(oid,h,"OPEN","{}"))
            self.store.conn.commit()
        return {"ok": True, "observation_id": oid}

    def settle_symbol(self, symbol: str, candles: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        daily=sorted((dict(c) for c in candles or []),key=lambda c:str(c.get("timestamp") or c.get("ts") or ""))
        rows=self.store.conn.execute("SELECT * FROM institutional_signal_observations WHERE symbol=?",(symbol.upper(),)).fetchall(); settled=0
        with self.store.write_lock:
            for raw in rows:
                obs=dict(raw); after=[c for c in daily if str(c.get("timestamp") or c.get("ts") or "")[:10]>obs["signal_date"]]
                entry=float(obs.get("entry_price") or 0)
                if not entry: continue
                for h in HORIZONS:
                    if len(after)<h: continue
                    close=float(after[h-1].get("close") or 0)
                    if not close: continue
                    ret=close/entry-1.0; day=str(after[h-1].get("timestamp") or after[h-1].get("ts") or "")[:10]
                    payload={"entry":entry,"exit":close,"signal_date":obs["signal_date"],"outcome_date":day,"horizon_days":h}
                    self.store.conn.execute("UPDATE institutional_signal_outcomes SET outcome_date=?,forward_return=?,status='SETTLED',payload_json=?,updated_at=CURRENT_TIMESTAMP WHERE observation_id=? AND horizon_days=?",(day,ret,json.dumps(payload),obs["observation_id"],h)); settled+=1
            self.store.conn.commit()
        return {"ok":True,"settled":settled}

    def performance(self) -> Dict[str, Any]:
        rows=self.store.conn.execute("""SELECT o.*,s.hidden_accumulation,s.volume_climax,s.absorption,s.stage
          FROM institutional_signal_outcomes o JOIN institutional_signal_observations s USING(observation_id)
          WHERE o.status='SETTLED'""").fetchall()
        groups=[]
        for signal,col in (("Hidden Accumulation","hidden_accumulation"),("Volume Climax","volume_climax"),("Absorption","absorption")):
            for h in HORIZONS:
                vals=[float(r["forward_return"]) for r in rows if r[col] and int(r["horizon_days"])==h and r["forward_return"] is not None]
                groups.append({"signal":signal,"horizon_days":h,"occurrences":len(vals),"win_rate":round(sum(v>0 for v in vals)/len(vals)*100,2) if vals else None,
                               "average_return_pct":round(statistics.fmean(vals)*100,3) if vals else None,"median_return_pct":round(statistics.median(vals)*100,3) if vals else None,
                               "state":"measured" if len(vals)>=30 else "collecting_evidence"})
        return {"ok":True,"groups":groups,"policy":"Performance is descriptive until purged walk-forward approval passes."}
