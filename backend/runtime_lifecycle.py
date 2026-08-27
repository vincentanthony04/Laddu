from __future__ import annotations

from runtime_shared import *


class RuntimeLifecycleMixin:
    """Startup, worker ownership, subscriptions, settlement and runtime lifecycle."""

    def _identity_startup_ready(self) -> bool:
        rows = [
            dict(self.status.get("instruments") or {}),
            dict(getattr(self, "_instrument_health_meta", {}) or {}),
        ]
        # The PostgreSQL catalogue is the authority.  Startup must not wait for
        # an asynchronous status projection when the authoritative proof is
        # already ready and the local search projection has been rebuilt.
        try:
            proof = dict(self.store.instrument_universe_stats() or {})
            if proof:
                rows.append({
                    "loaded": int(proof.get("active_total") or 0) > 0,
                    "cache_usable": self.store.instrument_count() > 0,
                    "count": int(proof.get("active_total") or 0),
                    "universe_revision": proof.get("revision"),
                    "universe_stats": proof,
                })
        except Exception:
            pass
        for meta in rows:
            stats = dict(meta.get("universe_stats") or {})
            if (
                meta.get("loaded") is True
                and meta.get("cache_usable") is True
                and int(meta.get("count") or 0) > 0
                and meta.get("universe_revision") == ACTIVE_UNIVERSE_REVISION
                and int(stats.get("nse_equities") or 0) > 0
                and int(stats.get("bse_only_equities") or 0) > 0
                and int(stats.get("indices") or 0) > 0
                and int(stats.get("derivatives") or 0) == 0
                and int(stats.get("out_of_policy_rows") or 0) == 0
            ):
                return True
        return False

    def _universe_startup_ready(self) -> bool:
        delivery = list((getattr(self, "_scanner_snapshot_rows", {}) or {}).get("delivery") or [])
        intraday = list((getattr(self, "_scanner_snapshot_rows", {}) or {}).get("intraday") or [])
        proof = dict((self.status.get("universe_authority") or {}).get("snapshots") or {})
        delivery_count = int((proof.get("delivery") or {}).get("population_count") or 0)
        intraday_count = int((proof.get("intraday") or {}).get("population_count") or 0)
        canonical_count = int((self.status.get("universe_authority") or {}).get("canonical_stocks") or 0)
        return (
            canonical_count > 0
            and 0 < len(delivery) <= canonical_count
            and 0 < len(intraday) <= canonical_count
            and delivery_count == len(delivery)
            and intraday_count == len(intraday)
        )

    def _update_startup_phase(self, phase: str, state: str, **detail: Any) -> None:
        transition_at = now_iso()
        with self.lock:
            current = dict(self.status.get("startup_phases") or {})
            self.status["startup_phases"] = apply_startup_phase_update(
                current, phase=phase, state=state, updated_at=transition_at, detail=detail,
            )
        self.health_registry.publish_runtime(self.status, state="fresh")

    def mark_http_ready(self) -> None:
        self._http_ready_event.set()
        self._update_startup_phase("http", "READY", port=PORT, bind_host=BIND_HOST)

    def _workers_startup_ready(self, names: tuple[str, ...]) -> tuple[bool, list[str]]:
        snapshot = self.supervisor.snapshot()
        blockers: list[str] = []
        for name in names:
            row = dict(snapshot.get(name) or {})
            if row.get("started") is not True:
                blockers.append(f"{name}:NOT_STARTED")
            elif row.get("alive") is not True:
                blockers.append(f"{name}:NOT_ALIVE")
            elif row.get("entered") is not True:
                blockers.append(f"{name}:FUNCTION_NOT_ENTERED")
            elif row.get("stale") is True:
                blockers.append(f"{name}:STALE")
            elif row.get("last_error"):
                blockers.append(f"{name}:ERROR:{str(row.get('last_error'))[:120]}")
        return not blockers, blockers

    def _start_optional_bulk_workers(self) -> None:
        """Start hydration/research workers after installation safety is ready.

        These workers remain fully observable.  A failure is reported as
        DEGRADED on the bulk phase, but never rewrites a safe required startup
        to BLOCKED or delays Windows installation acceptance.
        """
        self._update_startup_phase(
            "bulk", "STARTING", installation_blocking=False,
            reason="REQUIRED_STARTUP_COMPLETE_BULK_WARMING",
        )
        if not CONTROL.running or not self.supervisor.running:
            self._update_startup_phase("bulk", "DEGRADED", reason="RUNTIME_STOPPING", installation_blocking=False)
            return
        time.sleep(1.0)
        try:
            started = self.supervisor.start(list(self._startup_bulk_workers))
        except Exception as exc:
            self._update_startup_phase(
                "bulk", "DEGRADED", reason="BULK_WORKER_START_FAILED",
                error=str(exc)[:240], installation_blocking=False,
            )
            return
        deadline = time.monotonic() + 15.0
        ready, blockers = self._workers_startup_ready(self._startup_bulk_workers)
        while CONTROL.running and self.supervisor.running and not ready and time.monotonic() < deadline:
            time.sleep(0.25)
            ready, blockers = self._workers_startup_ready(self._startup_bulk_workers)
        self._update_startup_phase(
            "bulk", "READY" if ready else "DEGRADED",
            started=list(started), blockers=blockers,
            reason="" if ready else "BULK_WORKERS_WARMING_OR_DEGRADED",
            installation_blocking=False,
        )

    def _startup_phase_coordinator(self) -> None:
        # All required startup waits fit comfortably inside the installer's
        # 180-second wall clock.  Each timeout names the exact failing phase.
        if not self._http_ready_event.wait(timeout=20.0):
            self._update_startup_phase("http", "BLOCKED", reason="HTTP_SERVER_NOT_READY_WITHIN_20_SECONDS")
            return

        critical_deadline = time.monotonic() + 15.0
        critical_ready, critical_blockers = self._workers_startup_ready(self._startup_critical_workers)
        while CONTROL.running and self.supervisor.running and not critical_ready and time.monotonic() < critical_deadline:
            time.sleep(0.25)
            critical_ready, critical_blockers = self._workers_startup_ready(self._startup_critical_workers)
        if not critical_ready:
            self._update_startup_phase(
                "critical", "BLOCKED", reason="CRITICAL_WORKERS_NOT_READY_WITHIN_15_SECONDS",
                blockers=critical_blockers,
            )
            return
        self._update_startup_phase("critical", "READY", workers=list(self._startup_critical_workers))

        identity_deadline = time.monotonic() + 45.0
        while CONTROL.running and self.supervisor.running and not self._identity_startup_ready() and time.monotonic() < identity_deadline:
            time.sleep(0.5)
        if not self._identity_startup_ready():
            self._update_startup_phase(
                "operational", "BLOCKED",
                reason="FOCUSED_INSTRUMENT_IDENTITY_NOT_READY_WITHIN_45_SECONDS",
            )
            return

        # Freeze the current canonical desk populations.  Liquidity remains
        # scheduling/ranking evidence only; the operational startup gate must
        # accept the full supported cash universe rather than resurrecting the
        # retired <=1500 Delivery cap.
        if not self._universe_startup_ready():
            try:
                self.freeze_authoritative_universe()
            except Exception as exc:
                self.record_error("universe_startup", str(exc))
        if not self._universe_startup_ready():
            self._update_startup_phase(
                "operational", "BLOCKED",
                reason="CANONICAL_BOUNDED_DESK_UNIVERSE_NOT_READY",
                universe=dict(self.status.get("universe_authority") or {}),
            )
            return

        try:
            started = self.supervisor.start(list(self._startup_operational_workers))
        except Exception as exc:
            self._update_startup_phase(
                "operational", "BLOCKED", reason="OPERATIONAL_WORKER_START_FAILED", error=str(exc)[:240],
            )
            return
        self._update_startup_phase("operational", "STARTING", started=list(started))
        operational_deadline = time.monotonic() + 15.0
        operational_ready, operational_blockers = self._workers_startup_ready(self._startup_operational_workers)
        while CONTROL.running and self.supervisor.running and not operational_ready and time.monotonic() < operational_deadline:
            time.sleep(0.25)
            operational_ready, operational_blockers = self._workers_startup_ready(self._startup_operational_workers)
        if not operational_ready:
            self._update_startup_phase(
                "operational", "BLOCKED", reason="OPERATIONAL_WORKERS_NOT_READY_WITHIN_15_SECONDS",
                blockers=operational_blockers,
            )
            return
        self._update_startup_phase(
            "operational", "READY", started=list(started),
            universe=dict(self.status.get("universe_authority") or {}),
        )
        # Candidate 20 prewarms retained technical snapshots once after the
        # required runtime is healthy but before optional bulk research starts.
        # This is asynchronous and never delays the HTTP/operational READY gate.
        try:
            from core.technical_snapshot_service import TechnicalSnapshotService
            TechnicalSnapshotService(self).prewarm_retained()
        except Exception as exc:
            self.record_error("technical_snapshot_prewarm", str(exc))
        # apply_startup_phase_update now publishes state=COMPLETE because all
        # required phases are READY.  Bulk startup continues independently.
        threading.Thread(
            target=self._start_optional_bulk_workers,
            name="ProjectLadduOptionalBulkStartup",
            daemon=True,
        ).start()

    def start(self):
        self._set_status("service", "running")
        if not self._production_data_plane_active:
            # Compatibility/test mode preserves the deterministic old startup so
            # regressions do not need external PostgreSQL/QuestDB services.
            self.supervisor.start_all()
            return
        started = self.supervisor.start(list(self._startup_critical_workers))
        self._update_startup_phase("critical", "STARTING", started=list(started))
        threading.Thread(
            target=self._startup_phase_coordinator,
            name="ProjectLadduStartupPhaseCoordinator",
            daemon=True,
        ).start()

    def load_delivery_data_files(self) -> Dict[str, Any]:
        """Incrementally import local NSE delivery CSV files.

        Historical delivery files are immutable for normal operation.  Re-reading
        and re-parsing every historical CSV on each maintenance cycle used to
        compete with scanner work even though the durable repository was already
        populated.  A small persistent file watermark now makes unchanged files a
        true cache hit.  Content is hashed only when file metadata changes; exact
        same-content replacements are also skipped.
        """
        import hashlib

        patterns = ("sec_bhavdata_full_*.csv", "delivery_data.csv", "nse_delivery.csv", "delivery_*.csv")
        files = []
        for pattern in patterns:
            files.extend(sorted(DATA_DIR.glob(pattern)))
        seen, unique = set(), []
        for p in files:
            key = str(p.resolve()).lower()
            if key not in seen:
                seen.add(key); unique.append(p)

        manifest_path = DATA_DIR / ".delivery_import_watermarks.json"
        manifest: Dict[str, Any] = {"version": 1, "files": {}}
        try:
            if manifest_path.is_file():
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("files"), dict):
                    manifest = raw
        except Exception as exc:
            self.event("WARN", "delivery_data", "Delivery import watermark unreadable; rebuilding safely", {"error": str(exc)[:180]})

        entries = manifest.setdefault("files", {})
        total = 0
        reused_rows = 0
        imported_files: List[str] = []
        reused_files: List[str] = []
        same_content_files: List[str] = []
        failures = 0

        for path in unique:
            key = str(path.resolve()).lower()
            prior = dict(entries.get(key) or {})
            try:
                stat = path.stat()
                size = int(stat.st_size)
                mtime_ns = int(stat.st_mtime_ns)
                if (
                    prior.get("imported") is True
                    and int(prior.get("size") or -1) == size
                    and int(prior.get("mtime_ns") or -1) == mtime_ns
                ):
                    reused_files.append(path.name)
                    reused_rows += int(prior.get("rows_saved") or 0)
                    continue

                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if prior.get("imported") is True and str(prior.get("sha256") or "") == digest:
                    prior.update({"size": size, "mtime_ns": mtime_ns, "last_seen_at": now_iso()})
                    entries[key] = prior
                    same_content_files.append(path.name)
                    reused_rows += int(prior.get("rows_saved") or 0)
                    continue

                with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                    rows = [
                        {str(k or "").strip(): str(v or "").strip() for k, v in row.items()}
                        for row in csv.DictReader(handle)
                    ]
                saved = int(self.store.save_delivery_rows(rows, source=f"nse_csv:{path.name}") or 0)
                total += saved
                imported_files.append(path.name)
                entries[key] = {
                    "name": path.name, "size": size, "mtime_ns": mtime_ns,
                    "sha256": digest, "rows_saved": saved, "imported": True,
                    "imported_at": now_iso(), "last_seen_at": now_iso(),
                }
            except Exception as exc:
                failures += 1
                self.event("WARN", "delivery_data", "Delivery CSV import failed", {"file": path.name, "error": str(exc)[:180]})

        try:
            manifest.update({"version": 1, "updated_at": now_iso()})
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
            tmp.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            tmp.replace(manifest_path)
        except Exception as exc:
            self.event("WARN", "delivery_data", "Delivery import watermark persist failed", {"error": str(exc)[:180]})

        available = bool(unique) and (bool(imported_files) or bool(reused_files) or bool(same_content_files))
        refreshed = now_iso() if imported_files else self.status.get("last_delivery_refresh")
        return {
            "loaded": available,
            "rows": total,
            "rows_reused": reused_rows,
            "files": imported_files,
            "files_reused": reused_files,
            "files_same_content": same_content_files,
            "files_discovered": len(unique),
            "files_imported": len(imported_files),
            "files_skipped": len(reused_files) + len(same_content_files),
            "failures": failures,
            "last_refresh": refreshed,
            "data_dir": str(DATA_DIR),
            "watermark_file": str(manifest_path),
            "cache_first": True,
        }

    def ensure_live_delivery_data(self, force: bool = False) -> Dict[str, Any]:
        """Fetch latest NSE delivery report when available, then import local CSVs.

        NSE delivery is an exchange-published EOD report, not tick data. Upstox
        quotes/candles remain the live source during the session; this loop
        prevents Delivery analysis from depending on manual CSV drops.
        """
        sync_meta: Dict[str, Any] = {"ok": False, "auto_download": bool(NSE_DELIVERY_AUTO_DOWNLOAD), "state": "disabled"}
        if NSE_DELIVERY_AUTO_DOWNLOAD:
            try:
                sync_meta = self.nse_delivery.download_latest(force=force, lookback_days=NSE_DELIVERY_LOOKBACK_DAYS)
                sync_meta["backfill"] = self.nse_delivery.backfill_missing(lookback_days=NSE_DELIVERY_LOOKBACK_DAYS, max_downloads=24)
            except Exception as exc:
                sync_meta = {"ok": False, "auto_download": True, "state": "download_error", "message": str(exc)[:180], "checked_at": now_iso()}
                self.event("WARN", "delivery_data", "NSE delivery auto-download failed", sync_meta)
        imported = self.load_delivery_data_files()
        report_file = sync_meta.get("file") or ((imported.get("files") or [None])[-1])
        state = "ready" if imported.get("loaded") else ("download_pending" if NSE_DELIVERY_AUTO_DOWNLOAD else "manual_csv_pending")
        meta = {
            "loaded": bool(imported.get("loaded")), "rows": imported.get("rows", 0), "rows_reused": imported.get("rows_reused", 0),
            "files": imported.get("files", []), "files_reused": imported.get("files_reused", []),
            "files_same_content": imported.get("files_same_content", []), "files_discovered": imported.get("files_discovered", 0),
            "files_imported": imported.get("files_imported", 0), "files_skipped": imported.get("files_skipped", 0),
            "cache_first": bool(imported.get("cache_first")), "watermark_file": imported.get("watermark_file"),
            "last_refresh": imported.get("last_refresh"), "data_dir": imported.get("data_dir"),
            "auto_download": bool(NSE_DELIVERY_AUTO_DOWNLOAD), "download": sync_meta, "state": state,
            "last_report_date": sync_meta.get("report_date"), "last_file": report_file,
            "source": "nse_auto_download" if sync_meta.get("downloaded") else ("nse_cached_file" if sync_meta.get("cached") else "delivery_cache_reuse" if imported.get("files_skipped") and not imported.get("files_imported") else "local_csv_import"),
            "message": sync_meta.get("message") or (
                f"Delivery cache reused {int(imported.get('files_skipped') or 0)} unchanged file(s); no historical re-import"
                if imported.get("files_skipped") and not imported.get("files_imported") else
                "Delivery rows imported" if imported.get("loaded") else "NSE delivery evidence pending"
            ),
        }
        self._set_status("last_delivery_refresh", meta.get("last_refresh"))
        self._set_status("delivery_data_sync", {
            "state": meta.get("state"), "last_run": now_iso(), "last_report_date": meta.get("last_report_date"),
            "last_file": meta.get("last_file"), "source": meta.get("source"), "rows": meta.get("rows"),
            "rows_reused": meta.get("rows_reused"), "files_imported": meta.get("files_imported"),
            "files_skipped": meta.get("files_skipped"), "cache_first": meta.get("cache_first"),
            "message": meta.get("message"),
        })
        self.event("INFO" if meta.get("loaded") else "WARN", "delivery_data", "NSE delivery auto-sync completed" if meta.get("loaded") else "NSE delivery auto-sync pending", meta)
        return meta

    def delivery_data_loop(self, sup=None):
        time.sleep(4.0)
        while CONTROL.running and (sup is None or sup.running):
            if sup: sup.beat("delivery_data_sync")
            try:
                if sup:
                    sup.set_expected_idle("delivery_data_sync", False, waiting_on=None)
                    with sup.heartbeat_guard("delivery_data_sync"):
                        meta = self.ensure_live_delivery_data(force=False)
                else:
                    meta = self.ensure_live_delivery_data(force=False)
                sleep_for = NSE_DELIVERY_REFRESH_SECONDS if not is_india_market_open() else max(1800, NSE_DELIVERY_REFRESH_SECONDS)
                if not meta.get("loaded"):
                    sleep_for = min(sleep_for, 900)
                if sup:
                    sup.progress(
                        "delivery_data_sync", token=f"{meta.get('last_report_date')}:{meta.get('rows')}:{meta.get('state')}",
                        stage=str(meta.get("state") or "delivery_data"), completed_units=int(meta.get("rows") or 0),
                        total_units=None, waiting_on=f"next delivery-data cadence in {int(sleep_for)}s", expected_idle=True,
                    )
                time.sleep(sleep_for)
            except Exception as exc:
                self.record_error("delivery_data", str(exc), "nse_delivery_auto_sync")
                self._set_status("delivery_data_sync", {"state": "error", "last_run": now_iso(), "message": str(exc)[:180]})
                self.event("ERROR", "delivery_data", "Delivery data sync loop error", {"error": str(exc)[:180]})
                time.sleep(900)

    def delivery_context(self, symbol: str, record: bool = True) -> Dict[str, Any]:
        cache_key = str(symbol or "").upper().strip()
        cached = self._delivery_context_cache.get(cache_key)
        if not record and cached and time.time() - cached[0] < 900:
            return dict(cached[1])
        rows = self.store.latest_delivery(symbol, limit=65)
        inst = self._first_instrument(symbol)
        candles = self.store.get_candles(inst.get("instrument_key"), "day", limit=100) if inst and inst.get("instrument_key") else []
        result = analyze_institutional_signal(symbol, rows, candles)
        # Compatibility aliases for existing cards while every consumer moves
        # to the canonical institutional contract.
        delivery = result.get("delivery") or {}
        signals = result.get("signals") or {}
        result.update({
            "latest_pct": delivery.get("latest_pct"),
            "avg20_pct": delivery.get("average_20d_pct"),
            "qty_z20": delivery.get("deliverable_qty_z20"),
            "qty_expansion": bool(signals.get("hidden_accumulation") or signals.get("absorption")),
            "rows": rows[:5],
        })
        try:
            if not record:
                if result.get("state") == "ready":
                    InstitutionalOutcomeService(self.store).record(result)
                self._delivery_context_cache[cache_key] = (time.time(), dict(result))
                return result
            outcomes = InstitutionalOutcomeService(self.store)
            result["observation"] = outcomes.record(result)
            result["outcome_settlement"] = outcomes.settle_symbol(symbol, candles)
        except Exception as exc:
            result["outcome_error"] = str(exc)[:160]
        self._delivery_context_cache[cache_key] = (time.time(), dict(result))
        return result

    def reference_data_loop(self, sup=None):
        """v37.5 Phase 2/3: runs the daily NSE reference-data batch
        (delivery %, bulk/block deals) once per calendar
        day, shortly after market close. Deliberately its own loop with
        its own failure domain -- an NSE endpoint being down/changed for
        a day should show up in /api/system-health as one failed job,
        not take down quote/candle ingestion.
        """
        time.sleep(5.0)
        while CONTROL.running and (sup is None or sup.running):
            if sup: sup.beat("reference_data_daily")
            try:
                today = now_iso()[:10]
                mins_to_close = minutes_to_close()
                already_ran_today = (self._last_reference_run_date == today)
                should_run_now = (not already_ran_today) and (
                    not is_india_market_open() or (mins_to_close is not None and mins_to_close <= 5)
                )
                if should_run_now:
                    # Resolve the actual exchange session inside the reference-data
                    # authority.  Calendar weekends/holidays are scheduler dates,
                    # never NSE report dates.
                    if sup:
                        sup.set_expected_idle("reference_data_daily", False, waiting_on=None)
                        with sup.heartbeat_guard("reference_data_daily"):
                            result = self.reference_data.run_daily_job()
                    else:
                        result = self.reference_data.run_daily_job()
                    self._last_reference_run_date = today
                    if sup:
                        sup.progress("reference_data_daily", token=f"{today}:{getattr(result, 'get', lambda *_: None)('state') if result is not None else 'complete'}", stage="daily_reference_complete", completed_units=1, total_units=1, expected_idle=True, waiting_on="next eligible daily cycle")
                elif sup:
                    sup.set_expected_idle("reference_data_daily", True, waiting_on="next eligible daily cycle")
            except Exception as exc:
                self.record_error("reference_data_daily", str(exc)[:200])
                if sup:
                    sup.progress("reference_data_daily", token=f"error:{today}:{str(exc)[:80]}", stage="daily_reference_error", waiting_on="scheduled retry", expected_idle=True)
            time.sleep(300)

    def earnings_calendar_loop(self, sup=None):
        """v37.5 Phase 6: runs the daily NSE board-meeting/earnings-calendar
        fetch once per calendar day. Same shape as reference_data_loop --
        its own failure domain, shows up in /api/system-health as one job,
        never touches quote/candle ingestion."""
        time.sleep(7.0)
        while CONTROL.running and (sup is None or sup.running):
            if sup: sup.beat("earnings_calendar_daily")
            try:
                today = now_iso()[:10]
                mins_to_close = minutes_to_close()
                already_ran_today = (self._last_earnings_run_date == today)
                should_run_now = (not already_ran_today) and (
                    not is_india_market_open() or (mins_to_close is not None and mins_to_close <= 5)
                )
                if should_run_now:
                    result = self.earnings_calendar.run_daily_job()
                    self._last_earnings_run_date = today
                    if sup:
                        rows = int((result or {}).get("rows_written") or (result or {}).get("rows") or 0) if isinstance(result, dict) else 0
                        sup.progress("earnings_calendar_daily", token=f"{today}:{rows}", stage="earnings_calendar_complete", completed_units=rows, total_units=None, expected_idle=True, waiting_on="next eligible daily cycle")
                elif sup:
                    sup.set_expected_idle("earnings_calendar_daily", True, waiting_on="next eligible daily cycle")
            except Exception as exc:
                self.record_error("earnings_calendar_daily", str(exc)[:200])
                if sup:
                    sup.progress("earnings_calendar_daily", token=f"error:{today}:{str(exc)[:80]}", stage="earnings_calendar_error", waiting_on="scheduled retry", expected_idle=True)
            time.sleep(300)

    def instrument_bootstrap(self, sup=None):
        """Delegate continuous identity/readiness ownership to its failure domain."""
        from core.instrument_readiness_service import run_instrument_readiness
        return run_instrument_readiness(self, sup, running_fn=lambda: CONTROL.running, cleanup_symbols=INTELLIGENCE_SCAN_SYMBOLS)

    def freeze_authoritative_universe(self) -> Dict[str, Any]:
        """Build/freeze both desk populations once from the canonical catalogue."""
        lock = getattr(self, "_universe_freeze_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._universe_freeze_lock = lock
        with lock:
            return self._freeze_authoritative_universe_locked()

    def _freeze_authoritative_universe_locked(self) -> Dict[str, Any]:
        rows = self.store.all_authoritative_reference_rows(limit=10_000) or []
        current = getattr(self.store, "canonical_universe", None)
        if not isinstance(current, CanonicalUniverse):
            current = build_canonical_universe(rows, effective_date=india_now().date())
        previous = self._canonical_universe
        events = lifecycle_diff(previous, current) if isinstance(previous, CanonicalUniverse) else ()
        priority_symbols = {str(value or "").upper().strip() for value in self.store.priority_symbols_set()}
        governed_liquid_core = {str(value or "").upper().strip() for value in INTELLIGENCE_SCAN_SYMBOLS}
        delivery_authority = getattr(self.store, "production_delivery_repository", None)
        liquidity_reader = getattr(delivery_authority, "liquidity_ranked_symbols", None)
        # A large Parquet catalogue must never hold Windows startup hostage.
        # Run the analytical ranking in a daemon thread with a strict bound;
        # on timeout/error the governed liquid core is used and the reason is
        # published in the universe proof.  The late result is discarded.
        raw_liquidity: list[str] = []
        liquidity_query_state = "NOT_CONFIGURED"
        liquidity_query_error = ""
        if callable(liquidity_reader):
            holder: dict[str, Any] = {}
            done = threading.Event()
            def _read_liquidity() -> None:
                try:
                    holder["rows"] = list(liquidity_reader(limit=1500) or [])
                    holder["state"] = "READY"
                except Exception as exc:
                    holder["state"] = "ERROR"
                    holder["error"] = str(exc)[:240]
                finally:
                    done.set()
            threading.Thread(target=_read_liquidity, name="ProjectLadduBoundedLiquidityRead", daemon=True).start()
            if done.wait(timeout=8.0):
                liquidity_query_state = str(holder.get("state") or "ERROR")
                liquidity_query_error = str(holder.get("error") or "")
                raw_liquidity = list(holder.get("rows") or []) if liquidity_query_state == "READY" else []
            else:
                liquidity_query_state = "TIMEOUT_FALLBACK"
                liquidity_query_error = "PARQUET_LIQUIDITY_QUERY_EXCEEDED_8_SECONDS"
        liquidity_ranked: list[str] = []
        liquidity_seen: set[str] = set()
        for value in raw_liquidity:
            symbol = str(value or "").upper().strip()
            if not symbol or symbol in liquidity_seen:
                continue
            liquidity_seen.add(symbol)
            liquidity_ranked.append(symbol)
            if len(liquidity_ranked) >= 1500:
                break
        def bounded_symbols(primary: list[str], secondary: list[str], cap: int) -> list[str]:
            out: list[str] = []
            seen: set[str] = set()
            for value in list(primary) + list(secondary):
                symbol = str(value or "").upper().strip()
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                out.append(symbol)
                if len(out) >= cap:
                    break
            return out

        # Liquidity is scheduling/ranking evidence, NOT a Delivery universe filter.
        # The earlier bounded Delivery snapshot (<=1500 trailing-liquidity names)
        # made the UI show ~1,233 Delivery symbols while the canonical cash universe
        # contained >4,000 stocks.  That silently excluded newly active or currently
        # illiquid names before the scanner could record an explicit gate reason.
        # Both desks now begin from their complete supported canonical populations;
        # cheap screening/ranking decides which rows receive scarce deep analysis.
        liquidity_ready = len(liquidity_ranked) >= 100
        ordered_core = [str(value or "").upper().strip() for value in INTELLIGENCE_SCAN_SYMBOLS]
        ordered_priority = sorted(priority_symbols)
        delivery_base = liquidity_ranked if liquidity_ready else ordered_core
        delivery_ranked = bounded_symbols(ordered_priority, delivery_base, 1500)
        delivery_symbols = set(delivery_ranked)
        self._delivery_liquidity_rank_by_symbol = {symbol: index + 1 for index, symbol in enumerate(liquidity_ranked)}
        self._delivery_priority_symbols = set(priority_symbols)
        identity_proofs = {
            listing.security_id: canonical_listing_identity(listing)
            for listing in current.canonical_listings
        }
        delivery_eligibility = {
            listing.security_id: {
                # Full supported canonical Delivery population. Liquidity rank is
                # carried separately into scheduling evidence and must never make
                # a stock disappear from coverage before an explicit scanner gate.
                "eligible": True,
                "eligibility_reason": "DELIVERY_FULL_CANONICAL_SCREENING_POPULATION",
                "identity_verified": bool(identity_proofs[listing.security_id]["ok"]),
            }
            for listing in current.canonical_listings
        }
        intraday_eligibility = {
            listing.security_id: {
                "eligible": True,
                "eligibility_reason": "INTRADAY_FULL_CANONICAL_SCREENING_POPULATION",
                "identity_verified": bool(identity_proofs[listing.security_id]["ok"]),
            }
            for listing in current.canonical_listings
        }
        self._intraday_liquidity_rank_by_symbol = {symbol: index + 1 for index, symbol in enumerate(liquidity_ranked)}
        self._intraday_priority_symbols = set(priority_symbols)
        delivery = freeze_snapshot(current, desk="DELIVERY", effective_date=india_now().date(), eligibility=delivery_eligibility)
        intraday = freeze_snapshot(current, desk="INTRADAY", effective_date=india_now().date(), eligibility=intraday_eligibility)
        raw_by_provider_key = {
            str(raw.get("instrument_key") or raw.get("provider_instrument_key") or ""): dict(raw)
            for raw in rows
        }
        by_listing = {
            str(item.listing_id): raw_by_provider_key[item.provider_instrument_key]
            for item in current.canonical_listings
            if item.provider_instrument_key in raw_by_provider_key
        }
        self._canonical_universe = current
        self._universe_snapshots = {"delivery": delivery, "intraday": intraday}
        self._scanner_snapshot_rows = {
            "delivery": [dict(by_listing[key]) for key in delivery.listing_ids if key in by_listing],
            "intraday": [dict(by_listing[key]) for key in intraday.listing_ids if key in by_listing],
        }
        if self.universe_authority_repository is not None:
            self.universe_authority_repository.reconcile_universe(current, events)
            self.universe_authority_repository.persist_snapshot(delivery)
            self.universe_authority_repository.persist_snapshot(intraday)
        proof = {
            "rule_version": current.rule_version,
            "canonical_stocks": len(current.canonical_listings),
            "market_context": len(current.market_context),
            "exclusions": len(current.exclusions),
            "lifecycle_events": len(events),
            "snapshots": {
                "delivery": {"snapshot_id": delivery.snapshot_id, "population_count": delivery.population_count, "content_hash": delivery.content_hash},
                "intraday": {"snapshot_id": intraday.snapshot_id, "population_count": intraday.population_count, "content_hash": intraday.content_hash},
            },
            "authority": "POSTGRESQL" if self.universe_authority_repository is not None else "TEST_MEMORY_ONLY",
            "identity_authority": {
                "authority": "InstrumentIdentityAuthority",
                "authority_version": "1.1.0",
                "verified": sum(1 for row in identity_proofs.values() if row.get("ok")),
                "failed": sum(1 for row in identity_proofs.values() if not row.get("ok")),
            },
            "eligibility": {
                "intraday_screening_population": intraday.population_count,
                "intraday_starting_filter": "ALL_CANONICAL_INTRADAY_SERIES_IDENTITY_VERIFIED",
                "intraday_liquidity_is_ranking_evidence_not_universe_filter": True,
                "delivery_screening_population": delivery.population_count,
                "delivery_starting_filter": "ALL_CANONICAL_DELIVERY_SERIES_IDENTITY_VERIFIED",
                "delivery_liquidity_is_ranking_evidence_not_universe_filter": True,
                "delivery_liquidity_ranked": len(delivery_symbols),
                "raw_liquidity_rows": len(raw_liquidity),
                "deduplicated_liquidity_rows": len(liquidity_ranked),
                "priority_symbols": len(priority_symbols),
                "liquidity_metric": "60-session average traded value >= INR 5 crore with >=10 observations",
                "liquidity_authority": "PARQUET_DUCKDB",
                "delivery_population_ceiling": None,
                "delivery_population_bound": "canonical_stocks_and_supported_delivery_series_groups",
                "intraday_population_ceiling": None,
                "intraday_population_bound": "canonical_stocks_and_supported_intraday_series_groups",
                "fallback_used": not liquidity_ready,
                "liquidity_query_state": liquidity_query_state,
                "liquidity_query_error": liquidity_query_error,
                "startup_query_deadline_seconds": 8,
            },
            "time": now_iso(),
        }
        self.status["universe_authority"] = proof
        # Scanner progress is operational state bound to these exact immutable
        # desk snapshots. Reconcile now so no legacy checkpoint can reach the UI.
        try:
            self.scan_orchestration._ensure_checkpoint_reconciled()
            self.scan_orchestration._publish_scanner_progress("intraday")
            self.scan_orchestration._publish_scanner_progress("delivery")
        except Exception as exc:
            self.record_error("scan_checkpoint_reconcile", str(exc)[:200])
        return proof

    def immutable_scan_population(self, mode: str) -> List[Dict[str, Any]]:
        """Return the exact desk snapshot and reject cross-desk contamination.

        The v100 installed evidence showed a Delivery progress object using the
        167-row Intraday population while its snapshot contract claimed 1,222.
        A desk population is now validated against its immutable snapshot on
        every access.  Mismatches are fail-closed and trigger one deterministic
        rebuild from the canonical universe rather than silently scanning the
        wrong desk.
        """
        desk = str(mode or "").lower()
        rows = list(self._scanner_snapshot_rows.get(desk) or [])
        snapshot = (self._universe_snapshots or {}).get(desk)
        expected = int(getattr(snapshot, "population_count", 0) or 0)
        if expected and len(rows) != expected:
            self.record_error(
                f"{desk}_population_authority",
                f"desk snapshot mismatch rows={len(rows)} expected={expected}; refusing cross-desk population",
            )
            try:
                self.freeze_authoritative_universe(force=True)
            except TypeError:
                try:
                    self.freeze_authoritative_universe()
                except Exception:
                    pass
            except Exception:
                pass
            rows = list(self._scanner_snapshot_rows.get(desk) or [])
            snapshot = (self._universe_snapshots or {}).get(desk)
            expected = int(getattr(snapshot, "population_count", 0) or 0)
            if expected and len(rows) != expected:
                return []
        return rows

    def scanner_loop(self, sup=None):
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService.scanner_loop"""
        return self.scan_orchestration.scanner_loop(sup, running_fn=lambda: CONTROL.running)

    def _resolve_live_keys(self, symbols: list[str]) -> tuple[list[str], Dict[str, str]]:
        keys: list[str] = []
        symbol_by_key: Dict[str, str] = {}
        for symbol in symbols:
            display = str(symbol or "").upper().strip()
            if not display:
                continue
            is_index = "NIFTY" in display or "SENSEX" in display or display == "INDIA VIX"
            try:
                inst = self._index_instrument_for_chart(display) if is_index else self.instrument_resolver.resolve(display)
            except Exception:
                inst = None
            if is_index and not (inst or {}).get("instrument_key"):
                fallback_alias = {
                    "NIFTY": "NIFTY", "NIFTY 50": "NIFTY",
                    "NIFTY BANK": "BANK", "BANKNIFTY": "BANK",
                    "SENSEX": "SENSEX", "INDIA VIX": "VIX",
                }.get(display, display)
                inst = heatmap_index_identity(fallback_alias)
            key = str((inst or {}).get("instrument_key") or "").strip()
            if not key or key in symbol_by_key:
                continue
            keys.append(key)
            symbol_by_key[key] = display
            self.live_market.register_identity(key, display)
        return keys, symbol_by_key

    def set_priority_live_subscriptions(self, symbols: list[str], mode: str = "full", ttl_seconds: int = 900) -> Dict[str, Any]:
        """Batch-escalate a bounded priority list to rich feed mode.

        Open positions, selected charts and the pre-qualified opening list share
        one subscription planner.  This avoids N plan rebuilds and never tries
        to request rich feed for the complete 4,365-instrument catalogue.
        """
        requested_mode = str(mode or "full").lower().strip()
        if requested_mode not in {"ltpc", "full", "full_d30"}:
            raise ValueError("mode must be ltpc, full or full_d30")
        expiry = time.time() + max(30, min(int(ttl_seconds or 900), 3600))
        accepted: list[str] = []
        for raw in symbols or []:
            symbol = str(raw or "").upper().strip()
            if not symbol or symbol in accepted:
                continue
            self._interactive_live_symbols[symbol] = (requested_mode, expiry)
            accepted.append(symbol)
        status = self._refresh_live_subscription_plan(force=True) if accepted else self.live_market.status()
        return {
            "ok": True, "symbols": accepted, "count": len(accepted),
            "mode": requested_mode, "expires_in_seconds": max(30, min(int(ttl_seconds or 900), 3600)),
            "live_market_gateway": status,
        }

    def set_interactive_live_subscription(self, symbol: str, mode: str = "full", ttl_seconds: int = 900) -> Dict[str, Any]:
        sym = str(symbol or "").upper().strip()
        requested_mode = str(mode or "full").lower().strip()
        if requested_mode not in {"ltpc", "full", "full_d30"}:
            raise ValueError("mode must be ltpc, full or full_d30")
        if not sym:
            raise ValueError("symbol is required")
        self._interactive_live_symbols[sym] = (requested_mode, time.time() + max(30, min(int(ttl_seconds or 900), 3600)))
        self._refresh_live_subscription_plan(force=True)
        return {"ok": True, "symbol": sym, "mode": requested_mode, "expires_in_seconds": max(30, min(int(ttl_seconds or 900), 3600)), "live_market_gateway": self.live_market.status()}

    def _refresh_live_subscription_plan(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if not force and now - float(self._live_plan_refreshed_at or 0.0) < 12.0:
            return self.live_market.status()
        self._live_plan_refreshed_at = now
        # Broad LTPC coverage drives market/heat/trend displays.  Rich mode is
        # reserved for active positions, Final candidates and selected charts.
        broad: list[str] = []
        def add(target: list[str], value: Any) -> None:
            symbol = str(value or "").upper().strip()
            if symbol and symbol not in target:
                target.append(symbol)
        # Market/sector direction is a first-class live input.  Subscribe the
        # canonical governed index catalogue on the LTPC lane so the workspace
        # can update index and sector direction from the same verified stream as
        # equities instead of waiting for a periodic REST heatmap refresh.
        try:
            from core.heatmap_index_catalog import canonical_index_rows
            for row in canonical_index_rows():
                add(broad, row.get("display_name") or row.get("trading_symbol"))
        except Exception:
            for symbol in ("NIFTY 50", "NIFTY BANK", "SENSEX", "INDIA VIX"):
                add(broad, symbol)
        for symbol in list(NIFTY50_CORE):
            add(broad, symbol)
        for symbol in self._quote_delta_symbols(limit=300):
            add(broad, symbol)

        rich: list[str] = []
        try:
            for row in self.model_portfolio.open_positions():
                add(rich, row.get("symbol"))
        except Exception:
            pass
        try:
            for row in self.store.selected_signals("all", limit=40):
                add(rich, row.get("symbol"))
        except Exception:
            pass
        for symbol, (_mode, expiry) in list(self._interactive_live_symbols.items()):
            if expiry <= now:
                self._interactive_live_symbols.pop(symbol, None)
            else:
                add(rich, symbol)

        broad_keys, _ = self._resolve_live_keys(broad)
        rich_keys, _ = self._resolve_live_keys(rich)
        d30_symbols = [symbol for symbol, (mode, expiry) in self._interactive_live_symbols.items() if expiry > now and mode == "full_d30"]
        d30_keys, _ = self._resolve_live_keys(d30_symbols)
        d30_set = set(d30_keys)
        rich_keys = [key for key in rich_keys if key not in d30_set]
        rich_set = set(rich_keys)
        ltpc_keys = [key for key in broad_keys if key not in rich_set and key not in d30_set]
        status = self.live_market.set_plan(ltpc=ltpc_keys, full=rich_keys, full_d30=d30_keys)
        with self.lock:
            self.status["live_market_gateway"] = status
        return status

    def quote_delta_loop(self, sup=None):
        """Maintain stream subscriptions and use HTTP only as degraded fallback."""
        time.sleep(2.0)
        while CONTROL.running and (sup is None or sup.running):
            if sup: sup.beat("quote_delta")
            try:
                self._refresh_live_subscription_plan()
                gateway = self.live_market.status()
                with self.lock:
                    self.status["live_market_gateway"] = gateway
                if sup:
                    quote_state = dict(gateway.get("quotes") or {})
                    cursor_now = int(quote_state.get("cursor") or 0)
                    market_open_now = is_india_market_open()
                    sup.progress(
                        "quote_delta", token=f"{cursor_now}:{gateway.get('last_message_at')}:{gateway.get('operational_state')}",
                        stage="stream_subscription_reconciliation", current_item=str(gateway.get("operational_state") or "unknown"),
                        completed_units=cursor_now, total_units=None,
                        waiting_on=("market closed" if not market_open_now else None), expected_idle=not market_open_now,
                    )
                if is_india_market_open() and not gateway.get("connected"):
                    syms = self._quote_delta_symbols(limit=60)
                    if syms:
                        self.live_quotes(",".join(syms), allow_cached=False)
                    time.sleep(3)
                else:
                    time.sleep(1 if is_india_market_open() else 30)
            except Exception as exc:
                self.record_error("quote_delta", str(exc))
                with self.lock:
                    self.status.setdefault("quote_delta", {}).update({"state": "degraded", "last_run": now_iso(), "error": str(exc)[:160]})
                time.sleep(5)

    def _quote_delta_symbols(self, limit: int = 30) -> list[str]:
        out = []
        def add(sym):
            s = str(sym or "").upper().strip()
            if s and s not in out:
                out.append(s)
        for s in ("NIFTY 50", "SENSEX", "NIFTY BANK", "NIFTY"):
            add(s)
        try:
            for r in self.store.selected_signals("all", limit=20):
                add(r.get("symbol"))
        except Exception:
            pass
        try:
            for r in self.model_portfolio.open_positions():
                add(r.get("symbol"))
        except Exception:
            pass
        # Broad discovery is cheap; visible leaders use exact-token,
        # provider-timestamped quote hydration before they may be labelled live.
        for symbol in visible_market_leader_symbols(self._coverage_quote_cache):
            add(symbol)
        try:
            for r in self.store.priority_list(limit=40):
                add(r.get("symbol"))
        except Exception:
            pass
        return out[:limit]

    def run_index_level_scan(self) -> Dict[str, Any]:
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService.run_index_level_scan"""
        return self.scan_orchestration.run_index_level_scan()

    def _live_priorities_for_mode(self, mode: str, cap: int) -> list[Dict[str, Any]]:
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService._live_priorities_for_mode"""
        return self.scan_orchestration._live_priorities_for_mode(mode, cap)

    def run_live_mode_scan(self, mode: str) -> Dict[str, Any]:
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService.run_live_mode_scan"""
        return self.scan_orchestration.run_live_mode_scan(mode)

    def run_deep_mode_scan(self, mode: str) -> Dict[str, Any]:
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService.run_deep_mode_scan"""
        return self.scan_orchestration.run_deep_mode_scan(mode)

    # v103: legacy quote/candle signal settlement was removed from the runtime.
    # ModelPaper positions are the only execution/settlement authority;
    # ModelPaperSettlementReconciliationService repairs the canonical projection.

    def auth_test(self, force: bool = False) -> Dict[str, Any]:
        try:
            result = self.client.preflight(force=force)
        except Exception as exc:
            result = {"ok": False, "time": now_iso(), "token": self.client.token_status(), "quote": {"ok": False}, "historical": {"ok": False, "error": str(exc)}, "message": str(exc)}
        token_ok = bool((result.get("token") or {}).get("ok"))
        quote_ok = result.get("quote", {}).get("ok")
        hist_ok = result.get("historical", {}).get("ok")
        qerr = str(result.get("quote", {}).get("error") or "")
        herr = str(result.get("historical", {}).get("error") or "")
        combined_err = (qerr + " " + herr).lower()
        parameter_or_permission = any(x in combined_err for x in ("bad parameter", "400", "permission", "403"))
        if not token_ok:
            state = "token_missing"
        elif quote_ok and hist_ok:
            state = "ok"
        elif hist_ok and not quote_ok:
            state = "historical_ok_quote_degraded"
        elif parameter_or_permission:
            state = "data_degraded"
        else:
            state = "api_degraded"
        message = result.get("message")
        if state in ("data_degraded", "api_degraded", "historical_ok_quote_degraded"):
            message = f"{message or 'API/data degraded'}; issue is isolated from dashboard and symbol decisions"
        self._set_status("auth", {
            "state": state,
            "quote_ok": quote_ok,
            "historical_ok": hist_ok,
            "last_test": result.get("time"),
            "message": message,
            "quote_error": qerr or None,
            "historical_error": herr or None,
        })
        if quote_ok:
            self.quote_blocked_until = 0.0
        if hist_ok:
            self.market_data.hist_blocked_until = 0.0
        return result

    def _fast_lane_priorities(self, cap: int, market_open: bool) -> list[Dict[str, Any]]:
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService._fast_lane_priorities"""
        return self.scan_orchestration._fast_lane_priorities(cap, market_open)

    def run_fast_lane(self):
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService.run_fast_lane"""
        return self.scan_orchestration.run_fast_lane()

    def run_deep_scan(self):
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService.run_deep_scan"""
        return self.scan_orchestration.run_deep_scan()

    def _store_premarket_candidates(self, d: Dict[str, Any]) -> int:
        """Create one next-session Intraday watch candidate from Delivery research.

        Candidate preparation never bypasses market-open freshness, ORB/VWAP/volume,
        risk-authority or final DecisionEngineService promotion gates.
        """
        try:
            if str(d.get("mode") or "").lower() != "delivery":
                return 0
            score = int(d.get("score") or 0)
            side_raw = str(d.get("side") or "").upper()
            if score < 58 or side_raw not in ("LONG", "SHORT", "BEARISH"):
                return 0
            side = "SHORT" if side_raw in ("SHORT", "BEARISH") else "LONG"
            if side == "LONG":
                waiting_for = "next-session ORB high / VWAP reclaim with volume"
                trigger = d.get("resistance") or "ORB high + VWAP hold"
                invalid = d.get("support") or "ORB low / support break"
                setup = "Pre-market Intraday long candidate; live validation required"
            else:
                waiting_for = "next-session ORB low / VWAP rejection with volume"
                trigger = d.get("support") or "ORB low + VWAP rejection"
                invalid = d.get("resistance") or "ORB high / resistance reclaim"
                setup = "Pre-market Intraday short candidate; live validation required"
            cand = dict(d)
            cand.update({
                "mode": "intraday", "side": side, "decision": "WATCH", "status": "WATCH",
                "entry": None, "t1": None, "t2": None, "sl": None, "rr": None,
                "setup": setup, "watch_type": "auto_premarket",
                "waiting_for": waiting_for, "trigger": trigger, "invalidation": invalid,
                "reason": f"Pre-market candidate from Delivery research: {waiting_for}. Historical context only; no live trade until market-open gates confirm. {d.get('reason','')}",
                "target_window": "next session: first 30–120 minutes only if live trigger confirms",
                "max_holding_period": "same session; exit before close",
                "thesis_expiry": "expires same day if ORB/VWAP trigger does not confirm",
                "review_cadence": "every 5–15 minutes during market hours",
                "time_window_reason": "Historical research identifies the battlefield; live quote, ORB, VWAP, liquidity and index/sector direction must validate before selection.",
                "trade_budget_note": "Intraday budget: normally 0–2 trades/day. This is watch only.",
                "production_policy_version": POLICY_VERSION,
            })
            self.store.save_decision(cand)
            return 1
        except Exception as exc:
            self.event("WARN", "premarket", "Premarket candidate creation skipped", {"error": str(exc)})
            return 0

