"""Cache-first official NSE evidence for live analysis and explanations.

Read-only/local: never downloads reports on an HTTP request.  R47 turns the
retained daily history into point-in-time surprise features (turnover, trades,
delivery %, delivered quantity) that may confirm/penalise a price-action setup.
These features never manufacture Entry/S/R prices.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import math, statistics, threading, time
from typing import Any, Dict
from core.storage_layout import StorageLayout

SERVICE_VERSION = "nse-official-live-evidence-1.1.0-surprise-confirmation"
SOURCE_FLAGS = {
    "bhavcopy":"nse_has_bhavcopy","delivery":"nse_has_delivery","security_master":"nse_has_security_master",
    "risk":"nse_has_risk","index_context":"nse_has_index_context","deal_events":"nse_has_deal_events",
    "corporate_action":"nse_has_corporate_action","filings":"nse_has_filings","surveillance":"nse_has_surveillance",
}

def _f(value:Any)->float|None:
    try:
        value=float(value); return value if math.isfinite(value) else None
    except (TypeError,ValueError): return None

def _pct(value:float|None,digits:int=1)->str: return "—" if value is None else f"{value:.{digits}f}%"

def _z(current:Any, history:list[Any])->float|None:
    cur=_f(current); vals=[x for x in (_f(v) for v in history) if x is not None]
    if cur is None or len(vals)<5: return None
    sd=statistics.pstdev(vals)
    if not math.isfinite(sd) or sd<=1e-12: return 0.0
    return round((cur-statistics.fmean(vals))/sd,4)

@dataclass
class NseOfficialEvidenceService:
    data_dir:Path
    cache_seconds:float=30.0
    def __post_init__(self)->None:
        self.layout=StorageLayout.from_data_dir(Path(self.data_dir)); self._lock=threading.RLock(); self._cache={}
    def latest(self,symbol:str,as_of:str|date|datetime|None=None)->Dict[str,Any]:
        symbol=str(symbol or "").strip().upper()
        if not symbol: return {"ok":False,"state":"SYMBOL_REQUIRED","version":SERVICE_VERSION}
        as_of_date=as_of.date().isoformat() if isinstance(as_of,datetime) else as_of.isoformat() if isinstance(as_of,date) else str(as_of or datetime.now(timezone.utc).date().isoformat())[:10]
        key=(symbol,as_of_date)
        with self._lock:
            cached=self._cache.get(key)
            if cached and time.monotonic()-cached[0] <= self.cache_seconds: return dict(cached[1],cache="HIT")
        payload=self._read(symbol,as_of_date)
        with self._lock: self._cache[key]=(time.monotonic(),payload)
        return dict(payload,cache="MISS")

    def _read(self,symbol:str,as_of_date:str)->Dict[str,Any]:
        if not self.layout.analytics_db.is_file():
            return {"ok":False,"state":"RESEARCH_CATALOG_UNAVAILABLE","version":SERVICE_VERSION,"symbol":symbol,"as_of":as_of_date,"insights":[],"risk_blocks":[],"missing_sources":list(SOURCE_FLAGS),"decision_features":{}}
        try:
            import duckdb
            db=duckdb.connect(str(self.layout.analytics_db),read_only=True)
            try:
                views={r[0] for r in db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
                if "curated_nse_daily_features" not in views:
                    return {"ok":False,"state":"OFFICIAL_FEATURE_VIEW_UNAVAILABLE","version":SERVICE_VERSION,"symbol":symbol,"as_of":as_of_date,"insights":[],"risk_blocks":[],"missing_sources":list(SOURCE_FLAGS),"decision_features":{}}
                cur=db.execute("""SELECT * FROM curated_nse_daily_features WHERE upper(symbol)=? AND TRY_CAST(trade_date AS DATE)<=CAST(? AS DATE) ORDER BY TRY_CAST(trade_date AS DATE) DESC LIMIT 21""",[symbol,as_of_date])
                rows=cur.fetchall(); cols=[d[0] for d in cur.description]
                daily=[dict(zip(cols,r)) for r in rows]
                delivery=[]
                if "curated_delivery" in views:
                    dc=db.execute("""SELECT trade_date,delivery_pct,deliverable_qty,traded_qty FROM curated_delivery WHERE upper(symbol)=? AND trade_date<=CAST(? AS DATE) ORDER BY trade_date DESC LIMIT 21""",[symbol,as_of_date])
                    delivery=[dict(zip([d[0] for d in dc.description],r)) for r in dc.fetchall()]
            finally: db.close()
        except Exception as exc:
            return {"ok":False,"state":"OFFICIAL_EVIDENCE_READ_FAILED","version":SERVICE_VERSION,"symbol":symbol,"as_of":as_of_date,"error":str(exc),"insights":[],"risk_blocks":[],"missing_sources":list(SOURCE_FLAGS),"decision_features":{}}
        if not daily:
            return {"ok":False,"state":"NO_POINT_IN_TIME_OFFICIAL_EVIDENCE","version":SERVICE_VERSION,"symbol":symbol,"as_of":as_of_date,"insights":[],"risk_blocks":[],"missing_sources":list(SOURCE_FLAGS),"decision_features":{}}
        values=daily[0]; history=daily[1:21]
        current_delivery=delivery[0] if delivery else {}
        delivery_history=delivery[1:21] if len(delivery)>1 else []
        decision_features={
            "nse_turnover_z20":_z(values.get("nse_turnover"),[r.get("nse_turnover") for r in history]),
            "nse_trades_z20":_z(values.get("nse_number_of_trades"),[r.get("nse_number_of_trades") for r in history]),
            "delivery_pct_surprise":_z(current_delivery.get("delivery_pct",values.get("nse_delivery_pct")),[r.get("delivery_pct") for r in delivery_history] or [r.get("nse_delivery_pct") for r in history]),
            "delivered_quantity_surprise":_z(current_delivery.get("deliverable_qty"),[r.get("deliverable_qty") for r in delivery_history]),
            "delivery_pct":_f(current_delivery.get("delivery_pct",values.get("nse_delivery_pct"))),
            "deliverable_qty":_f(current_delivery.get("deliverable_qty")),
            "traded_qty":_f(current_delivery.get("traded_qty")),
        }
        available={name: _f(values.get(column)) is not None for name,column in SOURCE_FLAGS.items()}
        # A dedicated delivery row is also valid evidence even if the compact
        # source flag was absent/zero in one daily-feature projection.
        if current_delivery: available["delivery"]=True
        missing=[name for name,present in available.items() if not present]
        insights=[]; confirmations=[]; risk_blocks=[]
        delivery_pct=decision_features["delivery_pct"]
        if available["delivery"] and delivery_pct is not None:
            text=f"Official NSE delivery participation is {_pct(delivery_pct)} for the latest admitted session."; insights.append({"kind":"participation","tone":"positive" if delivery_pct>=50 else "neutral","text":text})
            if delivery_pct>=50: confirmations.append(text)
        for key,label in (("delivery_pct_surprise","delivery %"),("delivered_quantity_surprise","delivered quantity"),("nse_turnover_z20","turnover"),("nse_trades_z20","trade count")):
            z=decision_features.get(key)
            if z is not None and abs(z)>=.75:
                tone="positive" if z>0 else "negative"; text=f"NSE {label} surprise is {z:+.2f}σ versus the prior 20 admitted sessions."
                insights.append({"kind":"participation_surprise","tone":tone,"text":text})
                if z>0: confirmations.append(text)
        impact=_f(values.get("nse_impact_cost")); var_margin=_f(values.get("nse_var_margin"))
        if available["risk"]:
            insights.append({"kind":"execution_risk","tone":"negative" if impact is not None and impact>1.0 else "neutral","text":f"NSE execution evidence: impact cost {_pct(impact,2)}, VaR {_pct(var_margin,2)}."})
            if impact is not None and impact>2.0: risk_blocks.append("official_nse_impact_cost_exceeds_2pct")
        surveillance=bool(_f(values.get("nse_surveillance_flag")) or 0)
        if available["surveillance"]:
            if surveillance:
                risk_blocks.append("official_nse_surveillance_restriction_active"); insights.append({"kind":"surveillance","tone":"negative","text":"Official NSE surveillance evidence is active; new-entry authority is blocked."})
            else: insights.append({"kind":"surveillance","tone":"neutral","text":"No active surveillance flag is present in the latest admitted NSE report."})
        index_weight=_f(values.get("nse_index_weight")); beta=_f(values.get("nse_beta"))
        if available["index_context"] and (index_weight is not None or beta is not None):
            insights.append({"kind":"index_context","tone":"neutral","text":f"Point-in-time index context: weight {_pct(index_weight,2)}, beta {beta:.2f}." if beta is not None else f"Point-in-time index weight is {_pct(index_weight,2)}."})
        signed_deal=_f(values.get("nse_signed_deal_qty")); short_qty=_f(values.get("nse_short_qty"))
        if available["deal_events"] and (signed_deal is not None or short_qty is not None):
            tone="positive" if (signed_deal or 0)>0 else "negative" if (signed_deal or 0)<0 else "neutral"; insights.append({"kind":"event_pressure","tone":tone,"text":f"Official deal flow: signed bulk/block quantity {int(signed_deal or 0):,}; reported short quantity {int(short_qty or 0):,}."})
        promoter=_f(values.get("nse_promoter_holding_pct")); ownership_change=_f(values.get("nse_ownership_change_pct"))
        if available["filings"] and (promoter is not None or ownership_change is not None): insights.append({"kind":"ownership","tone":"positive" if (ownership_change or 0)>0 else "negative" if (ownership_change or 0)<0 else "neutral","text":f"Point-in-time filing evidence: promoter holding {_pct(promoter,2)}, ownership change {_pct(ownership_change,2)}."})
        high52=_f(values.get("nse_high_52w")); low52=_f(values.get("nse_low_52w"))
        if high52 is not None or low52 is not None: insights.append({"kind":"range_context","tone":"neutral","text":f"Official 52-week range authority: low ₹{low52:,.2f} · high ₹{high52:,.2f}." if low52 is not None and high52 is not None else "Official 52-week range evidence is partially available."})
        lineage=str(values.get("nse_source_lineage") or "")
        return {"ok":True,"state":"POINT_IN_TIME_OFFICIAL_EVIDENCE_READY" if not missing else "POINT_IN_TIME_OFFICIAL_EVIDENCE_PARTIAL","version":SERVICE_VERSION,"symbol":symbol,"as_of":str(values.get("trade_date") or as_of_date),
                "available_sources":available,"source_family_count":int(_f(values.get("nse_source_family_count")) or sum(available.values())),"missing_sources":missing,"lineage":[v for v in lineage.split(",") if v],
                "values":values,"decision_features":decision_features,"insights":insights,"confirmations":confirmations,"risk_blocks":risk_blocks,"production_model_ready":not missing,
                "policy":"LOCAL_POINT_IN_TIME_READ_ONLY_NO_HTTP_PROVIDER_IO; official NSE data confirms/penalises price-action evidence but never manufactures support/resistance/entry prices"}
