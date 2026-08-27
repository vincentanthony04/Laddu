from __future__ import annotations

import csv
import json
import mimetypes
import os
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

import application_runtime as runtime
from core.production_mode_policy import normalise_mode

class Handler(BaseHTTPRequestHandler):
    server_version = f"ProjectLaddu/{runtime.APP_VERSION}"

    @staticmethod
    def _diagnostic_location(exc: BaseException) -> str:
        """P0-05: every route-level 500/503 previously logged only
        str(exc), with no file/line/function. That made an installed-only
        capital-WFA 503 undiagnosable from evidence collection -- the
        operator could see THAT it failed, never WHERE. Return the deepest
        backend-code frame ("module.py:line in func"), never the full
        traceback, so this stays a pointer, not a leak.
        """
        try:
            frames = traceback.extract_tb(exc.__traceback__)
            for frame in reversed(frames):
                if "/backend/" in frame.filename.replace("\\", "/") or "backend" in frame.filename:
                    return f"{os.path.basename(frame.filename)}:{frame.lineno} in {frame.name}"
            if frames:
                last = frames[-1]
                return f"{os.path.basename(last.filename)}:{last.lineno} in {last.name}"
        except Exception:
            pass
        return "unknown_location"


    def _send_security_headers(self, *, content_type: str = ""):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        # The frontend uses inline style attributes for data-driven colour only;
        # scripts and network requests remain same-origin.
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if content_type.startswith("application/json") or content_type.startswith("text/csv"):
            self.send_header("Cache-Control", "no-store")

    def _same_origin_request(self) -> bool:
        origin = str(self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        try:
            parsed = urlparse(origin)
            return parsed.scheme in ("http", "https") and parsed.netloc.lower() == str(self.headers.get("Host") or "").lower()
        except Exception:
            return False

    def do_OPTIONS(self):
        if not self._same_origin_request():
            return self.json({"error": "cross_origin_request_blocked"}, 403)
        self.send_response(204)
        self._send_security_headers()
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        started = time.time()
        try:
            self._do_GET_inner()
        finally:
            elapsed_ms = int((time.time() - started) * 1000)
            try:
                runtime.APP.http_latency_monitor.record("GET", self.path, elapsed_ms)
            except Exception:
                pass
            if elapsed_ms >= 1200:
                runtime.log_line(f"WARN [perf] slow GET {self.path} took {elapsed_ms}ms")

    def _do_GET_inner(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        # v65.8.3: log receipt of market-intelligence requests BEFORE dispatch,
        # not just on completion. Every other log line in this handler (the
        # http access log, and the "slow GET" WARN in do_GET's finally) only
        # fires once the handler function returns -- so a genuine hang (the
        # call never returns: lock contention, an unbounded wait, an infinite
        # loop) currently produces zero log output, indistinguishable from
        # "request never arrived". This one line turns that silence into
        # proof-of-receipt, so if the hang recurs we can at least see which
        # requests started and infer, from what shows up after, how far each
        # one got before getting stuck.
        if parsed.path == "/api/market-intelligence":
            runtime.log_line(f"INFO [http] market-intelligence request received {parsed.query}")
        try:
            path = parsed.path
            # C22 ultra-thin immutable read-model fast path. The Market Radar
            # producer already publishes fully encoded compact JSON bytes. Do not
            # make a customer read compete for workload-governor/route-dispatch
            # Python work when that exact atomic projection is available. Full
            # diagnostic requests deliberately stay on the normal route.
            if path == "/api/market-radar":
                detail = str(qs.get("detail", [""])[0] or "").strip().lower()
                cached_bytes = getattr(runtime.APP, "_market_radar_http_bytes", None)
                if detail not in {"full", "diagnostic", "debug"} and isinstance(cached_bytes, (bytes, bytearray)) and cached_bytes:
                    return self.json({"_cached_json_bytes": bytes(cached_bytes)})
            if path in {"/api/market-radar", "/api/stock-snapshot", "/api/chart-data", "/api/historical", "/api/system-health"}:
                try:
                    governor = getattr(runtime.APP, "workload_governor", None)
                    if governor is not None:
                        governor.activate_surface(path, ttl_seconds=3.0)
                except Exception:
                    pass
            mode_raw = (qs.get("mode", ["all"])[0] or "all").lower(); mode = normalise_mode(mode_raw)
            q = str(qs.get("q", qs.get("query", [""]))[0] or "")
            handler = runtime.ROUTES_GET.get(path) or runtime.match_prefix_get(path)
            if handler:
                result = handler(runtime.APP, qs, q, mode)
                if isinstance(result, dict) and result.get("_response") == "csv":
                    return self.csv_response(
                        result.get("rows") or [], result.get("columns") or [],
                        result.get("filename") or "project-laddu-export.csv",
                    )
                if isinstance(result, tuple):
                    body, status = result
                    return self.json(body, status=status)
                return self.json(result)
            return self.static(path)
        except Exception as exc:
            location = self._diagnostic_location(exc)
            runtime.APP.event("ERROR", "http", "GET failed", {
                "path": self.path, "error": str(exc)[:500], "error_type": type(exc).__name__,
                "error_location": location, "traceback": traceback.format_exc()[-4000:],
            })
            return self.json({"error": str(exc), "error_type": type(exc).__name__, "error_location": location}, 500)

    def do_POST(self):
        started = time.time()
        try:
            self._do_POST_inner()
        finally:
            elapsed_ms = int((time.time() - started) * 1000)
            try:
                runtime.APP.http_latency_monitor.record("POST", self.path, elapsed_ms)
            except Exception:
                pass
            if elapsed_ms >= 1200:
                runtime.log_line(f"WARN [perf] slow POST {self.path} took {elapsed_ms}ms")

    def _do_POST_inner(self):
        parsed = urlparse(self.path)
        try:
            if not self._same_origin_request():
                return self.json({"error": "cross_origin_request_blocked"}, 403)
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > runtime.MAX_REQUEST_BODY_BYTES:
                return self.json({"error": "request_body_too_large", "max_bytes": runtime.MAX_REQUEST_BODY_BYTES}, 413)
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body or "{}")

            handler = runtime.ROUTES_POST.get(parsed.path)
            if handler:
                result = handler(runtime.APP, data)
                if isinstance(result, tuple):
                    resp_body, status = result
                    return self.json(resp_body, status=status)
                return self.json(result)
            return self.json({"error": "Not found"}, 404)
        except Exception as exc:
            location = self._diagnostic_location(exc)
            runtime.APP.event("ERROR", "http", "POST failed", {
                "path": self.path, "error": str(exc)[:500], "error_type": type(exc).__name__,
                "error_location": location, "traceback": traceback.format_exc()[-4000:],
            })
            return self.json({"error": str(exc), "error_type": type(exc).__name__, "error_location": location}, 500)

    def static(self, path: str):
        if path == "/" or not path:
            path = "/index.html"
        rel = path.lstrip("/")
        target = (runtime.FRONTEND_DIR / rel).resolve()
        try:
            if target.is_dir():
                target = (target / "index.html").resolve()
            if not str(target).startswith(str(runtime.FRONTEND_DIR.resolve())) or not target.exists() or not target.is_file():
                return self.json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self._send_security_headers(content_type=ctype)
            self.send_header("Cache-Control", "no-store" if target.name.endswith((".html", ".js", ".css")) else "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            return self.json({"error": str(exc)}, 500)

    def json(self, obj: Any, status: int = 200):
        cached = obj.get("_cached_json_bytes") if isinstance(obj, dict) else None
        if isinstance(cached, (bytes, bytearray)) and cached:
            # Market Radar and other future immutable read models may publish
            # already-encoded bytes. Keep per-request telemetry/internal markers
            # out of the customer payload rather than serializing again.
            data = bytes(cached)
        else:
            if isinstance(obj, dict) and "_cached_json_bytes" in obj:
                obj = {key: value for key, value in obj.items() if key != "_cached_json_bytes"}
            data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._send_security_headers(content_type="application/json")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as exc:
            # v36.7.2: the client (browser fetch/AbortController) closed the
            # connection before we finished writing -- typically a superseded
            # retry, not a server bug. Previously this exception escaped json()
            # and was caught by the outer do_GET/do_POST handler, which then
            # tried to send ANOTHER response (a 500) on the same dead socket,
            # throwing a second WinError 10053 and logging a confusing
            # "200 then error then 500" sequence for a single request.
            runtime.log_line(f"WARN [http] client disconnected before response finished ({exc})")

    def csv_response(self, rows: list[Dict[str, Any]], columns: list[str], filename: str, status: int = 200):
        # v36.9.15: signal-ledger CSV export -- every column needed to answer
        # "when was this signal given, and did it hit target or fail" outside
        # the app (Excel/pandas), not just in the live UI.
        import io, csv as _csv
        buf = io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})
        data = buf.getvalue().encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self._send_security_headers(content_type="text/csv")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as exc:
            runtime.log_line(f"WARN [http] client disconnected before response finished ({exc})")

    def log_message(self, fmt, *args):
        runtime.log_line("HTTP " + (fmt % args))


def serve(app=runtime.APP, *, host: str | None = None, port: int | None = None) -> None:
    runtime.FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
    bind_host = host or runtime.BIND_HOST
    bind_port = int(port or runtime.PORT)
    Handler.app = app
    server = ThreadingHTTPServer((bind_host, bind_port), Handler)
    security_profile = "localhost-only" if bind_host in ("127.0.0.1", "::1", "localhost") else "explicit-network-bind"
    app.mark_http_ready()
    app.event("INFO", "http", "Project Laddu HTTP server running", {"port": bind_port, "bind_host": bind_host, "security_profile": security_profile})
    if security_profile != "localhost-only":
        app.event("WARN", "security", "Project Laddu is exposed beyond localhost; place it behind an authenticated reverse proxy", {"bind_host": bind_host})
    try:
        server.serve_forever()
    finally:
        server.server_close()
