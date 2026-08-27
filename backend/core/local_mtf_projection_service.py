from __future__ import annotations

"""Deterministic local-only multi-timeframe projection.

The service reads canonical candle storage and performs pure projections.  It is
explicitly forbidden from owning provider clients, scanners, controllers,
Research, or application-runtime callbacks.  This is the technical/MTF read
model authority used by Clean Core R4.
"""

from typing import Any, Dict, Iterable, List

from core.canonical_candle_projection_service import CanonicalCandleProjectionService
from core.market_clock import candle_staleness
from core.master_candle_service import evaluate_master_candle
from core.mtf_semantic_service import MtfSemanticService
from indicators import closes, support_resistance


class LocalMtfProjectionService:
    VERSION = "local-mtf-projection-2.3.0-three-base-series"
    FRAME_LABELS = ("1m", "3m", "5m", "15m", "30m", "1H", "4H", "1D", "1W", "1M")
    INTRADAY_STORAGE_INTERVALS = ("1m", "15m")
    STORAGE_INTERVALS = INTRADAY_STORAGE_INTERVALS + ("1d",)
    INTRADAY_MATERIALIZATION_LIMIT = 720
    DAILY_MATERIALIZATION_LIMIT = 1500

    def __init__(self, store: Any):
        self.store = store
        self.projector = CanonicalCandleProjectionService()
        self.semantic = MtfSemanticService()

    def _read_many(
        self, instrument_key: str, *,
        intraday_limit: int | None = None,
        daily_limit: int | None = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Read the minimum canonical history needed for the ten-frame projection.

        Candidate 19 has exactly three canonical base families: 1m, 15m and
        daily. 3m/5m derive from 1m; 30m/1H/4H derive from 15m; 1W/1M derive
        from daily. The user-visible ten timeframes and every evidence family
        remain unchanged. This removes duplicate storage/provider work rather
        than mathematical depth. Daily stays at 1,500 bars for weekly/monthly
        and long-horizon performance evidence.
        """
        intraday_cap = max(260, int(intraday_limit or self.INTRADAY_MATERIALIZATION_LIMIT))
        daily_cap = max(750, int(daily_limit or self.DAILY_MATERIALIZATION_LIMIT))
        reader = getattr(self.store, "get_candles_many", None)
        raw: Dict[str, Any] = {}
        if callable(reader):
            try:
                raw = dict(reader(
                    instrument_key,
                    list(self.INTRADAY_STORAGE_INTERVALS),
                    limit=intraday_cap,
                    expand_sparse=False,
                ) or {})
            except TypeError:
                # Narrow adapters preserve the older signature. Production
                # storage accepts expand_sparse=False and therefore never
                # scans years of immutable intraday parts merely to fill an
                # arbitrary cap for an illiquid/newly-listed stock.
                raw = dict(reader(instrument_key, list(self.INTRADAY_STORAGE_INTERVALS), intraday_cap) or {})
            except Exception:
                raw = {}
        out: Dict[str, List[Dict[str, Any]]] = {}
        for interval in self.INTRADAY_STORAGE_INTERVALS:
            rows = raw.get(interval)
            if rows is None:
                try:
                    rows = self.store.get_candles(instrument_key, interval, limit=intraday_cap)
                except TypeError:
                    rows = self.store.get_candles(instrument_key, interval, intraday_cap)
                except Exception:
                    rows = []
            out[interval] = list(rows or [])[-intraday_cap:]
        try:
            daily = self.store.get_candles(instrument_key, "1d", limit=daily_cap)
        except TypeError:
            daily = self.store.get_candles(instrument_key, "1d", daily_cap)
        except Exception:
            daily = []
        out["1d"] = list(daily or [])[-daily_cap:]
        return out

    @staticmethod
    def _prefer_fresh(label: str, direct: Iterable[Dict[str, Any]], derived: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        candidates = []
        for priority, rows in ((1, list(direct or [])), (0, list(derived or []))):
            if not rows:
                continue
            freshness = candle_staleness(label, rows[-1])
            usable = not freshness.get("stale_candles") and freshness.get("usable_for_live_confirmation") is not False
            stamp = str(rows[-1].get("period_end") or rows[-1].get("bar_end") or rows[-1].get("timestamp") or "")
            candidates.append((1 if usable else 0, stamp, priority, rows))
        if not candidates:
            return []
        return max(candidates, key=lambda item: (item[0], item[1], item[2]))[3]

    def source_frames(
        self, instrument_key: str, *, intraday_limit: int | None = None, daily_limit: int | None = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        raw = self._read_many(instrument_key, intraday_limit=intraday_limit, daily_limit=daily_limit)
        completed = {
            "1m": self.projector.completed_chart(raw.get("1m") or [], "1m"),
            "3m": self.projector.completed_chart(raw.get("3m") or [], "3m"),
            "5m": self.projector.completed_chart(raw.get("5m") or [], "5m"),
            "15m": self.projector.completed_chart(raw.get("15m") or [], "15m"),
            "30m": self.projector.completed_chart(raw.get("30m") or [], "30m"),
            "60m": self.projector.completed_chart(raw.get("60m") or [], "1H"),
            "240m": self.projector.completed_chart(raw.get("240m") or [], "4H"),
            "1d": self.projector.completed_daily(raw.get("1d") or []),
        }
        # Operational continuity may use completed intraday data when official
        # daily storage has not appended the just-finished session.  The row is
        # explicitly non-Research authority.
        derived_daily = self.projector.derive_completed_session_daily(completed.get("1m") or [])
        completed["1d"] = self.projector.append_preferred_daily(completed.get("1d") or [], derived_daily)
        return completed

    def frame_rows(
        self, instrument_key: str, *, source: Dict[str, List[Dict[str, Any]]] | None = None
    ) -> List[tuple[str, List[Dict[str, Any]], str]]:
        source = dict(source or self.source_frames(instrument_key))
        base_1m = source.get("1m") or []
        base_15m = source.get("15m") or []
        derived_3m = self.projector.resample_intraday(base_1m, 3, source_minutes=1)
        derived_5m = self.projector.resample_intraday(base_1m, 5, source_minutes=1)
        derived_30m = self.projector.resample_intraday(base_15m, 30, source_minutes=15)
        derived_1h = self.projector.resample_intraday(base_15m, 60, source_minutes=15)
        derived_4h = self.projector.resample_intraday(base_15m, 240, source_minutes=15)
        weekly = self.projector.resample_weekly(source.get("1d") or [])
        monthly = self.projector.resample_monthly(source.get("1d") or [])
        return [
            ("1m", base_1m, "canonical_1m"),
            ("3m", derived_3m, "canonical_1m_resample"),
            ("5m", derived_5m, "canonical_1m_resample"),
            ("15m", base_15m, "canonical_15m"),
            ("30m", derived_30m, "canonical_15m_resample"),
            ("1H", derived_1h, "canonical_15m_resample"),
            ("4H", derived_4h, "canonical_15m_resample"),
            ("1D", source.get("1d") or [], "completed_day"),
            ("1W", weekly, "completed_day_to_week"),
            ("1M", monthly, "completed_day_to_month"),
        ]

    def project(
        self, instrument: Dict[str, Any], *, source: Dict[str, List[Dict[str, Any]]] | None = None
    ) -> List[Dict[str, Any]]:
        instrument_key = str((instrument or {}).get("instrument_key") or "").strip()
        if not instrument_key:
            return []
        results: List[Dict[str, Any]] = []
        for label, candles, source_name in self.frame_rows(instrument_key, source=source):
            try:
                values = closes(candles)
                freshness = candle_staleness(label, candles[-1] if candles else None)
                semantic = self.semantic.evaluate_frame(label, candles)
                state = str(semantic.get("state") or "PENDING").lower()
                if values and (freshness.get("stale_candles") or freshness.get("usable_for_live_confirmation") is False):
                    state = "stale"
                    semantic = {
                        **semantic,
                        "state": "STALE",
                        "direction": 0,
                        "score": 0,
                        "composite_score": 0,
                        "trend_score": 0,
                        "momentum_score": 0,
                        "participation_score": 0,
                        "structure_score": 0,
                        "quality_score": 0,
                        "confidence": 0,
                        "strength": 0,
                        "coverage": 0.0,
                        "reason": freshness.get("stale_message") or "completed candle is stale",
                    }
                metrics = semantic.get("metrics") or {}
                sr = support_resistance(candles, min(220, len(candles)))
                last = values[-1] if values else None
                directional_score = (
                    float(semantic.get("desk_directional_score"))
                    if semantic.get("desk_directional_score") is not None and state not in {"pending", "stale"}
                    else None
                )
                master_candle = (
                    evaluate_master_candle(candles, instrument_key=instrument_key, timeframe=label)
                    if label in {"1W", "1M"}
                    else None
                )
                results.append(
                    {
                        "tf": label,
                        "timeframe": label,
                        "state": state,
                        "close": round(float(last), 2) if last is not None else None,
                        "direction": semantic.get("direction", 0),
                        "strength": semantic.get("strength", 0),
                        "coverage": semantic.get("coverage", 0.0),
                        "rsi": metrics.get("rsi14"),
                        "adx": metrics.get("adx14"),
                        "ema9": metrics.get("ema9"),
                        "ema21": metrics.get("ema21"),
                        "ema50": metrics.get("ema50"),
                        "ema_state": metrics.get("ema_state"),
                        "macd": metrics.get("macd"),
                        "macd_signal": metrics.get("macd_signal"),
                        "macd_hist": metrics.get("macd_hist"),
                        "supertrend_direction": metrics.get("supertrend_direction"),
                        "supertrend_value": metrics.get("supertrend_value"),
                        "rvol20": metrics.get("rvol20"),
                        "roc5_pct": metrics.get("roc5_pct"),
                        "trend_score": semantic.get("trend_score"),
                        "momentum_score": semantic.get("momentum_score"),
                        "participation_score": semantic.get("participation_score"),
                        "structure_score": semantic.get("structure_score"),
                        "quality_score": semantic.get("quality_score"),
                        "composite_score": semantic.get("composite_score"),
                        "confidence": semantic.get("confidence"),
                        "directional_score": directional_score,
                        "component_directional_score": directional_score,
                        "indicator_coverage": semantic.get("coverage", 0.0),
                        "components": semantic.get("components"),
                        "support": sr.get("support"),
                        "resistance": sr.get("resistance"),
                        "last_candle": semantic.get("last_completed_at") or (candles[-1].get("timestamp") if candles else None),
                        "last_completed_at": semantic.get("last_completed_at"),
                        "source": source_name,
                        "count": len(values),
                        "completed_only": True,
                        "ema_periods": [9, 21, 50],
                        "semantic_model": semantic.get("semantic_model"),
                        "semantic_reason": semantic.get("reason"),
                        "master_candle": master_candle,
                        "session_partial_policy": "excluded from strict MTF and master-candle confirmation",
                        "projection_version": self.VERSION,
                        **freshness,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "tf": label,
                        "timeframe": label,
                        "state": "pending",
                        "reason": str(exc)[:160],
                        "source": source_name,
                        "projection_version": self.VERSION,
                    }
                )
        return results
