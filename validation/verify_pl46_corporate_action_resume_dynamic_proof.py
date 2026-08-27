"""P0-06 dynamic proof: sync_nse_corporate_action_history.sync() actually
resumes correctly across two runs when one chunk fails, rather than trusting
the source reading alone. This exercises the REAL sync() loop with a fake
fetcher/ingestion service and a real (tmp-dir) CorporateActionChunkManifest.

Run: python validation/verify_pl46_corporate_action_resume_dynamic_proof.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from tools.sync_nse_corporate_action_history import sync, FetchFailure  # noqa: E402


CSV_BODY = (
    b"SYMBOL,SERIES,EX_DATE,PURPOSE\n"
    b"TCS,EQ,15-Jan-2015,DIVIDEND - RS 5 PER SHARE\n"
)


class FakeIngestion:
    def __init__(self):
        self.ingested_ranges = []

    def ingest_bytes(self, *, source_key, trade_date, payload, filename, source_url, source_metadata):
        self.ingested_ranges.append((source_metadata["range_start"], source_metadata["range_end"]))
        return {
            "ok": True,
            "postgres_projection": {"state": "PROJECTED", "rows_projected": 1},
            "state": "INGESTED",
            "content_hash": "deadbeef",
            "curated_path": "curated/fake.csv",
        }


def _make_fetcher(fail_ranges):
    calls = {"count": 0}

    def fetcher(opener, chunk_start, chunk_end, *, user_agent):
        calls["count"] += 1
        key = (chunk_start.isoformat(), chunk_end.isoformat())
        if key in fail_ranges:
            raise FetchFailure("simulated NSE 503", retryable=True, http_status=503, error_code="NSE_5XX")
        ex_date = chunk_start.strftime("%d-%b-%Y")
        body = (
            b"SYMBOL,SERIES,EX_DATE,PURPOSE\n"
            + f"TCS,EQ,{ex_date},DIVIDEND - RS 5 PER SHARE\n".encode("ascii")
        )
        return {
            "payload": body,
            "content_type": "text/csv",
            "http_status": 200,
            "url": "https://www.nseindia.com/api/corporates-corporateActions",
        }

    return fetcher, calls


def test_failed_chunk_does_not_erase_earlier_success_and_resume_skips_published():
    from tools.sync_nse_corporate_action_history import _chunks

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        start, end = "2014-01-01", "2016-12-31"
        requested = [(a.isoformat(), b.isoformat()) for a, b in _chunks(start, end, 365)]
        total_chunks = len(requested)
        fail_key = requested[1]  # fail exactly one chunk in the middle
        fail_ranges = {fail_key}
        fetcher1, calls1 = _make_fetcher(fail_ranges)

        result1 = sync(
            data_dir=data_dir, operational_dsn="postgresql://fake/db",
            start=start, end=end, chunk_days=365,
            fetcher=fetcher1, ingestion_service=FakeIngestion(),
            sleep_fn=lambda s: None, random_fn=lambda: 0.0,
        )

        assert result1["published_chunks"] + result1["empty_valid_chunks"] == total_chunks - 1, (
            f"expected exactly {total_chunks - 1} chunks published, got: {result1}"
        )
        assert result1["failed_retryable_chunks"] == 1
        assert result1["progress_made"] is True
        assert result1["state"] == "RANGE_ACQUISITION_PARTIAL"
        assert result1["ok"] is False

        # The retryable chunk carries a real 15-minute cooldown (correct
        # behavior -- it must not hammer NSE every cycle). Advance it past
        # due so run 2 actually attempts it, rather than weakening the
        # cooldown logic itself.
        from core.corporate_action_chunk_manifest import CorporateActionChunkManifest
        from core.storage_layout import StorageLayout

        layout = StorageLayout.from_data_dir(data_dir)
        manifest = CorporateActionChunkManifest(layout)
        failed_row = manifest.load(
            source_family="corporate_actions", exchange="NSE",
            range_start=fail_key[0], range_end=fail_key[1],
            request_version="nse-corporate-action-request-2.0.0",
        )
        manifest.write({**failed_row, "next_retry_at": "2000-01-01T00:00:00+00:00"})

        # Second run: the failed chunk now succeeds. A fresh fetcher/ingestion is
        # used to prove the earlier chunks are NOT re-fetched (a network call for
        # them would itself be a resume failure, not just a correctness issue).
        # DB reconciliation itself needs a real PostgreSQL server, unavailable in
        # this sandbox -- stub it so this proves exactly what it can: chunk-level
        # resumability, the actual subject of P0-06. Reconciliation correctness
        # must be proven on the installed machine.
        import tools.reconcile_corporate_action_authority as recon_mod

        def fake_reconcile(*args, **kwargs):
            return {"ok": True, "coverage_rows_written": 1, "panel_symbols": 1, "complete_coverage_rows": 1}

        original_reconcile = recon_mod.reconcile
        recon_mod.reconcile = fake_reconcile
        try:
            fetcher2, calls2 = _make_fetcher(fail_ranges=set())  # nothing fails now
            ingestion2 = FakeIngestion()
            result2 = sync(
                data_dir=data_dir, operational_dsn="postgresql://fake/db",
                start=start, end=end, chunk_days=365,
                fetcher=fetcher2, ingestion_service=ingestion2,
                sleep_fn=lambda s: None, random_fn=lambda: 0.0,
            )
        finally:
            recon_mod.reconcile = original_reconcile

        assert calls2["count"] == 1, (
            f"resume must only fetch the previously-failed chunk, made {calls2['count']} network calls"
        )
        assert ingestion2.ingested_ranges == [list(fail_key)] or ingestion2.ingested_ranges == [fail_key], (
            f"resume must only re-ingest the previously-failed chunk, ingested: {ingestion2.ingested_ranges}"
        )
        assert result2["published_chunks"] + result2["empty_valid_chunks"] == total_chunks
        assert result2["failed_chunks"] == 0
        assert result2["complete_market_range"] is True


if __name__ == "__main__":
    test_failed_chunk_does_not_erase_earlier_success_and_resume_skips_published()
    print("PASS: P0-06 corporate-action resumable acquisition dynamic proof")
