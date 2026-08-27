"""Transactional PostgreSQL projection for validated official NSE observations.

Raw files and Parquet remain immutable evidence.  This repository projects only
source-schema-validated rows into queryable point-in-time operational/reference
tables.  It never grants model authority: source coverage and exact lineage are
still checked by training and walk-forward gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
import json


DAILY_SOURCES = {"cm_udiff_bhavcopy", "security_delivery_positions", "daily_volatility_var_price_band"}


def _is_membership_row(source_key: str, row: Mapping[str, Any]) -> bool:
    """Only genuine constituent rows may enter membership history.

    The legacy rich-index source can also contain index-level close rows.  Those
    remain valid immutable report evidence but are not constituent membership.
    """
    if source_key not in {"index_constituents", "index_snapshot_constituents_weights"}:
        return True
    symbol = str(row.get("symbol") or "").upper().strip()
    index_name = str(row.get("index_name") or "").upper().strip()
    if not symbol or not index_name:
        return False
    if source_key == "index_snapshot_constituents_weights" and symbol == index_name:
        return False
    return True


@dataclass
class NseOfficialPostgresRepository:
    dsn: str

    @staticmethod
    def _payload(row: Mapping[str, Any]) -> str:
        return json.dumps(dict(row), sort_keys=True, default=str)


    def historical_session_index(self, *, source_key: str = "cm_udiff_bhavcopy"):
        """Load positively observed official sessions without inferring holidays.

        A PROJECTED bhavcopy run with at least one projected row proves that the
        exchange published that trading session.  Absence proves nothing and is
        therefore left UNKNOWN by HistoricalSessionIndexAuthority.
        """
        import psycopg
        from core.historical_session_index_authority import HistoricalSessionIndexAuthority

        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT trade_date,source_key,content_hash
                         FROM reference.nse_official_ingestion_runs
                        WHERE source_key=%s AND state='PROJECTED' AND rows_projected>0
                        ORDER BY trade_date,content_hash""",
                    (str(source_key),),
                )
                rows = [
                    {"trade_date": row[0], "source_key": row[1], "content_hash": row[2]}
                    for row in cur.fetchall()
                ]
        return HistoricalSessionIndexAuthority.from_records(
            rows, source=f"POSTGRESQL_REFERENCE:{source_key}"
        )

    def project(
        self,
        *,
        source_key: str,
        trade_date: str,
        content_hash: str,
        source_url: str | None,
        source_filename: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        import psycopg

        projected = 0
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO reference.nse_official_ingestion_runs
                       (source_key,trade_date,content_hash,source_url,source_filename,row_count,state)
                       VALUES (%s,%s,%s,%s,%s,%s,'PROJECTING')
                       ON CONFLICT (source_key,trade_date,content_hash) DO UPDATE SET
                         source_url=EXCLUDED.source_url,source_filename=EXCLUDED.source_filename,
                         row_count=EXCLUDED.row_count,state='PROJECTING',projected_at=now()""",
                    (source_key, trade_date, content_hash, source_url, source_filename, len(rows)),
                )
                if source_key in DAILY_SOURCES:
                    sql = """INSERT INTO reference.nse_daily_security_facts
                        (source_key,trade_date,source_record_id,symbol,series,isin,open,high,low,close,
                         volume,turnover,number_of_trades,traded_qty,deliverable_qty,delivery_pct,
                         daily_volatility,var_margin,impact_cost,price_band_low,price_band_high,
                         published_at,content_hash,raw_payload)
                        VALUES (%(source_key)s,%(trade_date)s,%(source_record_id)s,%(symbol)s,%(series)s,%(isin)s,
                         %(open)s,%(high)s,%(low)s,%(close)s,%(volume)s,%(turnover)s,%(number_of_trades)s,
                         %(traded_qty)s,%(deliverable_qty)s,%(delivery_pct)s,%(daily_volatility)s,%(var_margin)s,
                         %(impact_cost)s,%(price_band_low)s,%(price_band_high)s,%(published_at)s,%(content_hash)s,
                         CAST(%(raw_payload)s AS jsonb))
                        ON CONFLICT (source_key,trade_date,source_record_id) DO UPDATE SET
                         symbol=EXCLUDED.symbol,series=EXCLUDED.series,isin=EXCLUDED.isin,open=EXCLUDED.open,
                         high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,volume=EXCLUDED.volume,
                         turnover=EXCLUDED.turnover,number_of_trades=EXCLUDED.number_of_trades,
                         traded_qty=EXCLUDED.traded_qty,deliverable_qty=EXCLUDED.deliverable_qty,
                         delivery_pct=EXCLUDED.delivery_pct,daily_volatility=EXCLUDED.daily_volatility,
                         var_margin=EXCLUDED.var_margin,impact_cost=EXCLUDED.impact_cost,
                         price_band_low=EXCLUDED.price_band_low,price_band_high=EXCLUDED.price_band_high,
                         published_at=EXCLUDED.published_at,content_hash=EXCLUDED.content_hash,
                         raw_payload=EXCLUDED.raw_payload,ingested_at=now()"""
                elif source_key == "mii_security_file":
                    sql = """INSERT INTO reference.nse_security_master_history
                        (trade_date,source_record_id,symbol,series,isin,instrument_name,listing_status,
                         eligible_universe,instrument_status,listing_date,published_at,content_hash,raw_payload)
                        VALUES (%(trade_date)s,%(source_record_id)s,%(symbol)s,%(series)s,%(isin)s,
                         %(instrument_name)s,%(listing_status)s,%(eligible_universe)s,%(instrument_status)s,
                         %(listing_date)s,%(published_at)s,%(content_hash)s,CAST(%(raw_payload)s AS jsonb))
                        ON CONFLICT (trade_date,source_record_id) DO UPDATE SET
                         symbol=EXCLUDED.symbol,series=EXCLUDED.series,isin=EXCLUDED.isin,
                         instrument_name=EXCLUDED.instrument_name,listing_status=EXCLUDED.listing_status,
                         eligible_universe=EXCLUDED.eligible_universe,instrument_status=EXCLUDED.instrument_status,
                         listing_date=EXCLUDED.listing_date,published_at=EXCLUDED.published_at,
                         content_hash=EXCLUDED.content_hash,raw_payload=EXCLUDED.raw_payload,ingested_at=now()"""
                elif source_key in {"index_constituents", "index_snapshot_constituents_weights"}:
                    sql = """INSERT INTO reference.nse_index_membership_history
                        (trade_date,source_record_id,index_name,symbol,isin,index_weight,index_return,
                         market_cap,free_float_market_cap,beta,sector_name,published_at,content_hash,raw_payload)
                        VALUES (%(trade_date)s,%(source_record_id)s,%(index_name)s,%(symbol)s,%(isin)s,
                         %(index_weight)s,%(index_return)s,%(market_cap)s,%(free_float_market_cap)s,
                         %(beta)s,%(sector_name)s,%(published_at)s,%(content_hash)s,CAST(%(raw_payload)s AS jsonb))
                        ON CONFLICT (trade_date,source_record_id) DO UPDATE SET
                         index_name=EXCLUDED.index_name,symbol=EXCLUDED.symbol,isin=EXCLUDED.isin,
                         index_weight=EXCLUDED.index_weight,index_return=EXCLUDED.index_return,
                         market_cap=EXCLUDED.market_cap,free_float_market_cap=EXCLUDED.free_float_market_cap,
                         beta=EXCLUDED.beta,sector_name=EXCLUDED.sector_name,published_at=EXCLUDED.published_at,
                         content_hash=EXCLUDED.content_hash,raw_payload=EXCLUDED.raw_payload,ingested_at=now()"""
                elif source_key == "filings_results_announcements_shareholding":
                    sql = """INSERT INTO reference.nse_filing_events
                        (trade_date,source_record_id,symbol,isin,filing_type,filing_period,filing_timestamp,
                         announcement_category,announcement_text,revenue,ebitda,net_profit,eps,
                         promoter_holding_pct,fii_holding_pct,dii_holding_pct,ownership_change_pct,
                         published_at,content_hash,raw_payload)
                        VALUES (%(trade_date)s,%(source_record_id)s,%(symbol)s,%(isin)s,%(filing_type)s,
                         %(filing_period)s,%(filing_timestamp)s,%(announcement_category)s,%(announcement_text)s,
                         %(revenue)s,%(ebitda)s,%(net_profit)s,%(eps)s,%(promoter_holding_pct)s,
                         %(fii_holding_pct)s,%(dii_holding_pct)s,%(ownership_change_pct)s,%(published_at)s,
                         %(content_hash)s,CAST(%(raw_payload)s AS jsonb))
                        ON CONFLICT (trade_date,source_record_id) DO UPDATE SET
                         symbol=EXCLUDED.symbol,isin=EXCLUDED.isin,filing_type=EXCLUDED.filing_type,
                         filing_period=EXCLUDED.filing_period,filing_timestamp=EXCLUDED.filing_timestamp,
                         announcement_category=EXCLUDED.announcement_category,
                         announcement_text=EXCLUDED.announcement_text,revenue=EXCLUDED.revenue,
                         ebitda=EXCLUDED.ebitda,net_profit=EXCLUDED.net_profit,eps=EXCLUDED.eps,
                         promoter_holding_pct=EXCLUDED.promoter_holding_pct,fii_holding_pct=EXCLUDED.fii_holding_pct,
                         dii_holding_pct=EXCLUDED.dii_holding_pct,ownership_change_pct=EXCLUDED.ownership_change_pct,
                         published_at=EXCLUDED.published_at,content_hash=EXCLUDED.content_hash,
                         raw_payload=EXCLUDED.raw_payload,ingested_at=now()"""
                else:
                    sql = """INSERT INTO reference.nse_market_events
                        (source_key,trade_date,source_record_id,symbol,isin,event_type,participant,
                         participant_category,deal_side,deal_type,deal_price,counterparty,bulk_qty,block_qty,
                         short_qty,margin_qty,ex_date,record_date,action_type,purpose,price_factor,volume_factor,
                         surveillance_flag,surveillance_category,high_52w,low_52w,price_band_change_pct,
                         published_at,content_hash,raw_payload)
                        VALUES (%(source_key)s,%(trade_date)s,%(source_record_id)s,%(symbol)s,%(isin)s,
                         %(event_type)s,%(participant)s,%(participant_category)s,%(deal_side)s,%(deal_type)s,
                         %(deal_price)s,%(counterparty)s,%(bulk_qty)s,%(block_qty)s,%(short_qty)s,%(margin_qty)s,
                         %(ex_date)s,%(record_date)s,%(action_type)s,%(purpose)s,%(price_factor)s,%(volume_factor)s,
                         %(surveillance_flag)s,%(surveillance_category)s,%(high_52w)s,%(low_52w)s,
                         %(price_band_change_pct)s,%(published_at)s,%(content_hash)s,CAST(%(raw_payload)s AS jsonb))
                        ON CONFLICT (source_key,trade_date,source_record_id) DO UPDATE SET
                         symbol=EXCLUDED.symbol,isin=EXCLUDED.isin,event_type=EXCLUDED.event_type,
                         participant=EXCLUDED.participant,participant_category=EXCLUDED.participant_category,
                         deal_side=EXCLUDED.deal_side,deal_type=EXCLUDED.deal_type,deal_price=EXCLUDED.deal_price,
                         counterparty=EXCLUDED.counterparty,bulk_qty=EXCLUDED.bulk_qty,block_qty=EXCLUDED.block_qty,
                         short_qty=EXCLUDED.short_qty,margin_qty=EXCLUDED.margin_qty,ex_date=EXCLUDED.ex_date,
                         record_date=EXCLUDED.record_date,action_type=EXCLUDED.action_type,purpose=EXCLUDED.purpose,
                         price_factor=EXCLUDED.price_factor,volume_factor=EXCLUDED.volume_factor,
                         surveillance_flag=EXCLUDED.surveillance_flag,
                         surveillance_category=EXCLUDED.surveillance_category,high_52w=EXCLUDED.high_52w,
                         low_52w=EXCLUDED.low_52w,price_band_change_pct=EXCLUDED.price_band_change_pct,
                         published_at=EXCLUDED.published_at,content_hash=EXCLUDED.content_hash,
                         raw_payload=EXCLUDED.raw_payload,ingested_at=now()"""

                for row in rows:
                    if source_key in {"index_constituents", "index_snapshot_constituents_weights"} and not _is_membership_row(source_key, row):
                        continue
                    params = dict(row)
                    params["event_type"] = source_key
                    params["raw_payload"] = self._payload(row)
                    cur.execute(sql, params)
                    projected += 1
                cur.execute(
                    """UPDATE reference.nse_official_ingestion_runs
                       SET state='PROJECTED',rows_projected=%s,projected_at=now()
                       WHERE source_key=%s AND trade_date=%s AND content_hash=%s""",
                    (projected, source_key, trade_date, content_hash),
                )
            conn.commit()
        return {"state": "PROJECTED", "rows_projected": projected, "authority": "POSTGRESQL"}
