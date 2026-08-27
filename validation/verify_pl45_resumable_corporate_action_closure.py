from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.corporate_action_adjustment_authority import CorporateActionAdjustmentAuthority
from core.corporate_action_chunk_manifest import CorporateActionChunkManifest
from core.storage_layout import StorageLayout
from tools import sync_nse_corporate_action_history as syncmod

checks = []
failures = []

def ck(name, cond, detail=None):
    row = {"name": name, "ok": bool(cond)}
    if detail is not None:
        row["detail"] = detail
    checks.append(row)
    if not cond:
        failures.append(row)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Protected mathematics/trainer bytes must remain identical to PL44.
ck("WFA_MATH_FROZEN", sha(BACKEND / "core" / "walk_forward_validation_service.py") == "caa06382df77a6c536232c5f3cec040f791297a790a603ee064fda1e6c474369")
ck("TRAINER_MATH_FROZEN", sha(BACKEND / "tools" / "train_nse_smart_model.py") == "785b796dd2696ddeb8632a9382f130f260ec69a0a96f34260898af9ed6f5f7a1")

app_runtime = (BACKEND / "application_runtime.py").read_text(encoding="utf-8-sig")
ck("SINGLE_EXISTING_SCHEDULER", app_runtime.count('self.supervisor.register("historical_pit_enrichment"') == 1)

sync_source = (BACKEND / "tools" / "sync_nse_corporate_action_history.py").read_text(encoding="utf-8-sig")
ck("CHUNK_MANIFEST_WIRED", "CorporateActionChunkManifest" in sync_source and "EMPTY_VALID" in sync_source and "PUBLISHED" in sync_source)
ck("NO_GLOBAL_FALSE_COMPLETE", "complete_coverage" in sync_source and "RANGE_ACQUISITION_PARTIAL" in sync_source)
ck("NSE_SESSION_WARMUP", "_warm(opener" in sync_source and "401, 403" in sync_source)
ck("RATE_LIMIT_RETRY_VISIBLE", "HTTP_429" in sync_source or "429" in sync_source)
ck("REQUEST_BUDGET", "PROJECT_LADDU_NSE_CA_REQUEST_BUDGET" in sync_source)
ck("DURABLE_RETRY_COOLDOWN", "manifest.retry_due(current)" in sync_source and "cooldown_chunks" in sync_source)
ck("PERMANENT_FAILURE_NOT_RETRIED", 'current_status == "FAILED_PERMANENT"' in sync_source)
ck("VALIDATED_RESUMES_WITHOUT_NETWORK", 'current_status in {"FETCHED", "VALIDATED"}' in sync_source and "resumed_without_network" in sync_source)

class FakeIngestion:
    def __init__(self):
        self.calls = []
    def ingest_bytes(self, **kwargs):
        self.calls.append(dict(kwargs))
        digest = hashlib.sha256(bytes(kwargs["payload"])).hexdigest()
        return {
            "ok": True,
            "state": "INGESTED",
            "content_hash": digest,
            "curated_path": "/fake/curated.parquet",
            "postgres_projection": {"state": "PROJECTED", "rows_projected": 1},
        }


def payload(symbol: str, ex_date: str, subject: str = "Bonus") -> bytes:
    return json.dumps({"data": [{"symbol": symbol, "exDate": ex_date, "subject": subject}]}).encode("utf-8")

with tempfile.TemporaryDirectory(prefix="pl45-ca-resume-") as td:
    data_dir = Path(td)
    layout = StorageLayout.from_data_dir(data_dir)
    layout.ensure()
    manifest = CorporateActionChunkManifest(layout)
    ingestion = FakeIngestion()

    # Avoid any external network during deterministic validation.
    old_new, old_warm = syncmod._new_opener, syncmod._warm
    syncmod._new_opener = lambda ua: object()
    syncmod._warm = lambda opener, ua: {"ok": True, "http_status": 200, "test": True}

    first_calls = []
    def first_fetch(opener, start, end, *, user_agent):
        key = (start.isoformat(), end.isoformat())
        first_calls.append(key)
        if start.isoformat() == "2020-01-01":
            return {"payload": payload("TCS", "15-Jan-2020"), "content_type": "application/json", "http_status": 200, "url": "test://nse"}
        if start.isoformat() == "2020-01-31":
            raise syncmod.FetchFailure("rate limited", retryable=True, http_status=429, error_code="HTTP_429")
        return {"payload": b'{"data":[]}', "content_type": "application/json", "http_status": 200, "url": "test://nse"}

    try:
        first = syncmod.sync(
            data_dir=data_dir, operational_dsn="postgresql://unused/test",
            start="2020-01-01", end="2020-03-30", chunk_days=30,
            fetcher=first_fetch, manifest=manifest, ingestion_service=ingestion,
            sleep_fn=lambda _: None, random_fn=lambda: 0.0,
        )
        ck("PARTIAL_RUN_RETURNS_EXPLICIT_PROGRESS", first.get("state") == "RANGE_ACQUISITION_PARTIAL" and first.get("progress_made") is True, first)
        ck("EARLIER_SUCCESS_PERSISTS", first.get("published_chunks") == 1 and first.get("empty_valid_chunks") == 1, first)
        ck("FAILED_CHUNK_VISIBLE", first.get("failed_chunks") == 1 and first.get("retryable_chunks") == 1, first.get("failures"))
        ck("NO_FALSE_COVERAGE_ON_PARTIAL", first.get("complete_coverage") is False and first.get("coverage_written") is False)

        # An immediate supervisor retry must honour the durable cooldown and make
        # zero NSE requests rather than hammering the endpoint every cycle.
        cooldown_calls = []
        def cooldown_fetch(*args, **kwargs):
            cooldown_calls.append(True)
            raise AssertionError("cooldown chunk must not be fetched")
        cooldown = syncmod.sync(
            data_dir=data_dir, operational_dsn="postgresql://unused/test",
            start="2020-01-01", end="2020-03-30", chunk_days=30,
            fetcher=cooldown_fetch, manifest=manifest, ingestion_service=ingestion,
            sleep_fn=lambda _: None, random_fn=lambda: 0.0,
        )
        ck("IMMEDIATE_RETRY_HONOURS_COOLDOWN", not cooldown_calls and cooldown.get("cooldown_chunks") == 1 and cooldown.get("requests_used") == 0, cooldown)

        # Simulate the next due supervisor attempt. Only the failed middle chunk
        # may be fetched; already-published and valid-empty chunks stay untouched.
        middle = manifest.load(source_family=syncmod.SOURCE_FAMILY, exchange=syncmod.EXCHANGE, range_start="2020-01-31", range_end="2020-02-29", request_version=syncmod.REQUEST_VERSION)
        manifest.write({**middle, "next_retry_at": "2000-01-01T00:00:00+00:00"})
        second_calls = []
        def second_fetch(opener, start, end, *, user_agent):
            second_calls.append((start.isoformat(), end.isoformat()))
            return {"payload": payload("INFY", "15-Feb-2020", "Split"), "content_type": "application/json", "http_status": 200, "url": "test://nse"}

        import tools.reconcile_corporate_action_authority as reconmod
        old_reconcile = reconmod.reconcile
        reconmod.reconcile = lambda *args, **kwargs: {
            "ok": True, "state": "ROW_SCOPED_AUTHORITY_READY",
            "coverage_rows_written": 2, "complete_coverage_rows": 2,
        }
        try:
            second = syncmod.sync(
                data_dir=data_dir, operational_dsn="postgresql://unused/test",
                start="2020-01-01", end="2020-03-30", chunk_days=30,
                fetcher=second_fetch, manifest=manifest, ingestion_service=ingestion,
                sleep_fn=lambda _: None, random_fn=lambda: 0.0,
            )
        finally:
            reconmod.reconcile = old_reconcile

        ck("RETRY_SKIPS_PUBLISHED_AND_EMPTY_VALID", second_calls == [("2020-01-31", "2020-02-29")], second_calls)
        ck("FULL_RANGE_RECONCILES_ONLY_AFTER_ALL_CHUNKS", second.get("state") == "RANGE_SYNC_AND_RECONCILIATION_COMPLETE" and second.get("complete_market_range") is True and second.get("coverage_written") is True, second)
        ck("RANGE_ATTESTATION_WRITTEN", (layout.manifests_dir / "corporate_actions" / "market-range-attestation.json").is_file())

        rows = manifest.list_for_range(
            source_family=syncmod.SOURCE_FAMILY, exchange=syncmod.EXCHANGE,
            request_version=syncmod.REQUEST_VERSION,
            requested=[("2020-01-01","2020-01-30"),("2020-01-31","2020-02-29"),("2020-03-01","2020-03-30")],
        )
        ck("VALID_EMPTY_IS_DURABLE_SUCCESS", any(row.get("status") == "EMPTY_VALID" and manifest.completed(row) for row in rows))
        ck("PUBLISHED_CHUNKS_HAVE_RAW_HASH_PROOF", all(row.get("payload_sha256") and Path(str(row.get("raw_evidence_path"))).is_file() for row in rows))

        # A third run is satisfied entirely by the range attestation; no fetch.
        third_calls = []
        def third_fetch(*args, **kwargs):
            third_calls.append(True)
            raise AssertionError("fetch should not run")
        reconmod.reconcile = lambda *args, **kwargs: {"ok": True, "state": "ROW_SCOPED_AUTHORITY_READY", "coverage_rows_written": 2}
        try:
            third = syncmod.sync(
                data_dir=data_dir, operational_dsn="postgresql://unused/test",
                start="2020-01-01", end="2020-03-30", chunk_days=30,
                fetcher=third_fetch, manifest=manifest, ingestion_service=ingestion,
                sleep_fn=lambda _: None, random_fn=lambda: 0.0,
            )
        finally:
            reconmod.reconcile = old_reconcile
        ck("COMPLETE_RANGE_REUSES_ATTESTATION", third.get("state") == "ALREADY_COVERED_RECONCILED" and not third_calls, third)
    finally:
        syncmod._new_opener, syncmod._warm = old_new, old_warm

# A parser/schema permanent failure must remain fail-closed without repeated network
# requests until REQUEST_VERSION changes.
with tempfile.TemporaryDirectory(prefix="pl45-ca-permanent-") as td:
    layout=StorageLayout.from_data_dir(Path(td)); layout.ensure(); man=CorporateActionChunkManifest(layout)
    ident=dict(source_family=syncmod.SOURCE_FAMILY,exchange=syncmod.EXCHANGE,range_start="2020-01-01",range_end="2020-01-30",request_version=syncmod.REQUEST_VERSION)
    man.write({**ident,"status":"FAILED_PERMANENT","attempt_count":1,"last_error_code":"INVALID_SCHEMA"})
    permanent_calls=[]
    perm=syncmod.sync(data_dir=Path(td),operational_dsn="postgresql://unused/test",start="2020-01-01",end="2020-01-30",chunk_days=30,fetcher=lambda *a,**k: permanent_calls.append(True),manifest=man,ingestion_service=FakeIngestion(),sleep_fn=lambda _:None,random_fn=lambda:0.0)
    ck("PERMANENT_FAILURE_STAYS_CLOSED", not permanent_calls and perm.get("permanent_failure_chunks")==1 and perm.get("requests_used")==0,perm)

# A validated transport checkpoint whose PostgreSQL projection previously failed
# must resume from its durable raw bytes and not redownload the NSE chunk.
with tempfile.TemporaryDirectory(prefix="pl45-ca-validated-resume-") as td:
    data_dir=Path(td); layout=StorageLayout.from_data_dir(data_dir); layout.ensure(); man=CorporateActionChunkManifest(layout)
    chunk_start,chunk_end=syncmod.date(2020,1,1),syncmod.date(2020,1,30)
    raw=payload("TCS","15-Jan-2020")
    validated=syncmod._validate_payload(payload=raw,content_type="application/json",chunk_start=chunk_start,chunk_end=chunk_end)
    raw_path=syncmod._raw_evidence_path(layout,chunk_start,chunk_end,validated["payload_sha256"],"application/json")
    syncmod._write_raw_evidence(raw_path,raw)
    ident=dict(source_family=syncmod.SOURCE_FAMILY,exchange=syncmod.EXCHANGE,range_start="2020-01-01",range_end="2020-01-30",request_version=syncmod.REQUEST_VERSION)
    man.write({**ident,"status":"VALIDATED","attempt_count":1,"last_http_status":200,"payload_sha256":validated["payload_sha256"],"row_count":1,"content_type":"application/json","source_url":"test://nse","raw_evidence_path":str(raw_path),"fetched_at":"2020-02-01T00:00:00+00:00","validated_at":"2020-02-01T00:00:01+00:00"})
    resume_calls=[]
    import tools.reconcile_corporate_action_authority as reconmod
    old_reconcile=reconmod.reconcile; reconmod.reconcile=lambda *a,**k:{"ok":True,"state":"ROW_SCOPED_AUTHORITY_READY","coverage_rows_written":1,"complete_coverage_rows":1}
    try:
        resumed=syncmod.sync(data_dir=data_dir,operational_dsn="postgresql://unused/test",start="2020-01-01",end="2020-01-30",chunk_days=30,fetcher=lambda *a,**k: resume_calls.append(True),manifest=man,ingestion_service=FakeIngestion(),sleep_fn=lambda _:None,random_fn=lambda:0.0)
    finally:
        reconmod.reconcile=old_reconcile
    ck("VALIDATED_CHECKPOINT_REPROJECTS_OFFLINE", not resume_calls and resumed.get("resumed_without_network")==1 and resumed.get("state")=="RANGE_SYNC_AND_RECONCILIATION_COMPLETE",resumed)

# Transport/schema safety: bot pages and malformed payloads never become valid empties.
try:
    syncmod._validate_payload(payload=b"<html>blocked</html>", content_type="text/html", chunk_start=syncmod.date(2020,1,1), chunk_end=syncmod.date(2020,1,30))
    bot_rejected = False
except syncmod.FetchFailure as exc:
    bot_rejected = exc.retryable and exc.error_code == "HTML_BLOCK_PAGE"
ck("HTML_BLOCK_PAGE_NEVER_EMPTY_VALID", bot_rejected)

try:
    syncmod._validate_payload(payload=b'{"foo":"bar"}', content_type="application/json", chunk_start=syncmod.date(2020,1,1), chunk_end=syncmod.date(2020,1,30))
    malformed_rejected = False
except syncmod.FetchFailure as exc:
    malformed_rejected = (not exc.retryable) and exc.error_code == "INVALID_SCHEMA"
ck("MALFORMED_SCHEMA_FAILS_CLOSED", malformed_rejected)

# Existing adjustment convention: historical candle before ex-date receives supplied factors.
auth = CorporateActionAdjustmentAuthority()
adjusted = auth.adjust_candle(
    {"date":"2020-01-01","open":100,"high":110,"low":90,"close":100,"volume":10},
    instrument_key="NSE_EQ|TCS",
    actions=[{"instrument_key":"NSE_EQ|TCS","verified":True,"ex_date":"2020-01-15","action_type":"SPLIT","price_factor":0.5,"volume_factor":2.0,"source_hash":"x"}],
    coverage={"instrument_key":"NSE_EQ|TCS","complete":True,"verified_at":"2020-02-01T00:00:00Z","coverage_start":"2019-01-01","coverage_end":"2020-12-31","source_hash":"c"},
)
ck("SPLIT_FACTOR_DIRECTION_PRESERVED", adjusted["close"] == 50.0 and adjusted["volume"] == 20.0, adjusted)
post = auth.adjust_candle(
    {"date":"2020-01-20","close":100,"volume":10},
    instrument_key="NSE_EQ|TCS",
    actions=[{"instrument_key":"NSE_EQ|TCS","verified":True,"ex_date":"2020-01-15","action_type":"SPLIT","price_factor":0.5,"volume_factor":2.0,"source_hash":"x"}],
    coverage={"instrument_key":"NSE_EQ|TCS","complete":True,"verified_at":"2020-02-01T00:00:00Z","coverage_start":"2019-01-01","coverage_end":"2020-12-31","source_hash":"c"},
)
ck("NO_POST_EX_DATE_BACKWARD_LEAK", post["close"] == 100.0 and post["volume"] == 10.0, post)

importer = (BACKEND / "tools" / "import_corporate_actions.py").read_text(encoding="utf-8-sig")
ck("MANUAL_BOOTSTRAP_PRESERVES_FILE_HASH", "source_file_sha256" in importer)
ck("MANUAL_BOOTSTRAP_SCOPED_SYMBOLS", "--coverage-symbol" in importer and "MANUAL_SCOPED_VERIFIED_RANGE" in importer)
ck("MANUAL_IMPORT_IDEMPOTENT", "ON CONFLICT (action_id) DO UPDATE" in importer and "ON CONFLICT (instrument_key) DO UPDATE" in importer)


# HTTP failure classification is explicit and never silently interpreted as empty.
from urllib.error import HTTPError, URLError
class RaisingOpener:
    def __init__(self, exc): self.exc = exc
    def open(self, request, timeout=0): raise self.exc
for code in (401, 403, 429):
    try:
        syncmod._fetch(RaisingOpener(HTTPError("https://nse", code, "x", {}, None)), syncmod.date(2020,1,1), syncmod.date(2020,1,30), user_agent="test")
        classified = False
    except syncmod.FetchFailure as exc:
        classified = exc.retryable and exc.http_status == code and exc.error_code == f"HTTP_{code}"
    ck(f"HTTP_{code}_IS_VISIBLE_RETRYABLE", classified)
try:
    syncmod._fetch(RaisingOpener(URLError("timeout")), syncmod.date(2020,1,1), syncmod.date(2020,1,30), user_agent="test")
    timeout_classified = False
except syncmod.FetchFailure as exc:
    timeout_classified = exc.retryable and exc.error_code == "NETWORK_OR_TIMEOUT"
ck("NETWORK_TIMEOUT_IS_VISIBLE_RETRYABLE", timeout_classified)

# Manual bootstrap can attest an explicit symbol/range without falsely marking the universe complete.
from tools import import_corporate_actions as impmod
import types
class FakeCursorResult:
    def __init__(self, row=None, rows=None): self._row=row; self._rows=rows or []
    def fetchone(self): return self._row
    def fetchall(self): return self._rows
class FakeConn:
    def __init__(self): self.executed=[]; self.committed=False
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params=()):
        self.executed.append((str(sql), tuple(params or ())))
        text=' '.join(str(sql).split()).lower()
        if 'from core.instruments' in text and 'upper(trading_symbol)=%s' in text:
            sym=str(params[0]).upper()
            return FakeCursorResult(row={"instrument_key":f"NSE_EQ|{sym}","exchange":"NSE","trading_symbol":sym})
        return FakeCursorResult()
    def commit(self): self.committed=True
fake_conn=FakeConn()
fake_psycopg=types.ModuleType('psycopg')
fake_psycopg.connect=lambda *args, **kwargs: fake_conn
fake_rows=types.ModuleType('psycopg.rows')
fake_rows.dict_row=object()
old_psy=sys.modules.get('psycopg'); old_rows=sys.modules.get('psycopg.rows')
sys.modules['psycopg']=fake_psycopg; sys.modules['psycopg.rows']=fake_rows
try:
    scoped=impmod.import_rows(
        dsn='postgresql://unused/test', rows=[], source_name='nse_csv_bootstrap',
        coverage_start='2019-01-01', coverage_end='2020-12-31', mark_complete=True,
        mark_universe_complete=False, source_file_sha256='a'*64, coverage_symbols=['TCS','INFY'],
    )
finally:
    if old_psy is None: sys.modules.pop('psycopg',None)
    else: sys.modules['psycopg']=old_psy
    if old_rows is None: sys.modules.pop('psycopg.rows',None)
    else: sys.modules['psycopg.rows']=old_rows
ck("SCOPED_BOOTSTRAP_COMPLETES_TCS_INFY_ONLY", scoped.get('coverage_complete') is True and scoped.get('universe_coverage_attested') is False and scoped.get('instruments') == 2, scoped)
coverage_inserts=[row for row in fake_conn.executed if 'insert into reference.corporate_action_coverage' in ' '.join(row[0].split()).lower()]
ck("SCOPED_BOOTSTRAP_WRITES_TWO_COVERAGE_ROWS", len(coverage_inserts)==2 and all('MANUAL_SCOPED_VERIFIED_RANGE' in row[1] for row in coverage_inserts), coverage_inserts)

row={"exchange":"NSE","trading_symbol":"TCS","ex_date":"2020-01-15","action_type":"SPLIT","price_factor":"0.5","volume_factor":"2"}
a=impmod.normalise(row,'nse_csv_bootstrap',source_file_sha256='b'*64)
b=impmod.normalise(row,'nse_csv_bootstrap',source_file_sha256='b'*64)
c=impmod.normalise(row,'nse_csv_bootstrap',source_file_sha256='c'*64)
ck("MANUAL_ROW_IDENTITY_IS_IDEMPOTENT", a['source_hash']==b['source_hash'])
ck("MANUAL_FILE_HASH_BINDS_PROVENANCE", a['source_hash']!=c['source_hash'] and a['source_file_sha256']=='b'*64)

reconcile_source=(BACKEND/'tools'/'reconcile_corporate_action_authority.py').read_text(encoding='utf-8-sig')
ck("RECONCILIATION_REMAINS_IDEMPOTENT", 'ON CONFLICT (action_id) DO UPDATE' in reconcile_source and 'ON CONFLICT (instrument_key) DO UPDATE' in reconcile_source)

history = (BACKEND / "core" / "historical_pit_sweep_service.py").read_text(encoding="utf-8-sig")
ck("SUPERVISOR_SURFACES_CHUNK_PROGRESS", all(token in history for token in ("published_chunks", "failed_chunks", "progress_made", "corporate_action_resume")))

result = {"ok": not failures, "version": "pl45-resumable-corporate-action-closure-1.0.0", "passed": len(checks)-len(failures), "failed": len(failures), "checks": checks}
print(json.dumps(result, indent=2, default=str))
raise SystemExit(0 if result["ok"] else 2)
