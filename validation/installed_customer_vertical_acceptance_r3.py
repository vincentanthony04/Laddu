"""Independent installed-browser acceptance for Project Laddu.

Candidate 24 retains the dependency-free Edge/CDP authority and adds bounded cleanup plus non-vacuous Workspace convergence proof.  The probe
launches the already-installed Microsoft Edge in headless mode with a temporary
profile and drives it through the Chrome DevTools Protocol (CDP) using only the
Python standard library.  The product never decides whether this probe passes.

The probe always writes JSON evidence and screenshots (where Edge progressed far
enough), including fatal launch/CDP errors, console exceptions and network
failures.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from exact_vertical_tracker import load as load_vertical_tracker, save as save_vertical_tracker, update as update_vertical_tracker, lifecycle_records as tracker_lifecycle_records, ids as tracker_ids


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def check(name: str, ok: bool, detail: Any = None) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        data = sock.recv(remaining)
        if not data:
            raise ConnectionError("CDP websocket closed unexpectedly")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


class WebSocket:
    """Minimal RFC6455 client sufficient for local Edge DevTools/CDP."""

    def __init__(self, url: str, timeout: float = 30.0):
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError(f"unsupported websocket scheme: {parsed.scheme}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        if parsed.scheme == "wss":
            import ssl
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock = raw
        self.sock.settimeout(timeout)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > 65536:
                break
        head = bytes(response).split(b"\r\n\r\n", 1)[0]
        first = head.split(b"\r\n", 1)[0]
        if b" 101 " not in first:
            raise ConnectionError(f"CDP websocket upgrade failed: {first.decode('latin-1', 'replace')}")
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        headers = {}
        for line in head.split(b"\r\n")[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode("latin-1").strip().lower()] = v.decode("latin-1").strip()
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("CDP websocket Sec-WebSocket-Accept mismatch")

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        mask = secrets.token_bytes(4)
        length = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        fragments = bytearray()
        message_opcode: int | None = None
        while True:
            first, second = _read_exact(self.sock, 2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", _read_exact(self.sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _read_exact(self.sock, 8))[0]
            mask = _read_exact(self.sock, 4) if masked else b""
            payload = _read_exact(self.sock, length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("CDP websocket closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                message_opcode = opcode
                fragments.extend(payload)
            elif opcode == 0x0 and message_opcode is not None:
                fragments.extend(payload)
            else:
                continue
            if fin:
                if message_opcode != 0x1:
                    raise ValueError("unexpected binary CDP websocket message")
                return fragments.decode("utf-8")

    def close(self) -> None:
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class CDP:
    def __init__(self, websocket_url: str):
        self.ws = WebSocket(websocket_url)
        self.next_id = 1
        self.events: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        ident = self.next_id
        self.next_id += 1
        payload = {"id": ident, "method": method}
        if params:
            payload["params"] = params
        self.ws.send_text(json.dumps(payload, separators=(",", ":")))
        deadline = time.monotonic() + timeout
        old_timeout = self.ws.sock.gettimeout()
        try:
            while time.monotonic() < deadline:
                self.ws.sock.settimeout(max(0.2, deadline - time.monotonic()))
                raw = self.ws.recv_text()
                msg = json.loads(raw)
                if msg.get("id") == ident:
                    if "error" in msg:
                        raise RuntimeError(f"CDP {method} failed: {msg['error']}")
                    return dict(msg.get("result") or {})
                if "method" in msg:
                    self.events.append(msg)
        finally:
            self.ws.sock.settimeout(old_timeout)
        raise TimeoutError(f"CDP command timed out: {method}")

    def evaluate(self, expression: str, *, timeout: float = 30.0) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True, "userGesture": True},
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(f"browser JS exception: {result['exceptionDetails']}")
        remote = dict(result.get("result") or {})
        if "value" in remote:
            return remote.get("value")
        if remote.get("type") == "undefined":
            return None
        return remote.get("description")

    def drain(self, seconds: float = 0.15) -> None:
        deadline = time.monotonic() + seconds
        old_timeout = self.ws.sock.gettimeout()
        try:
            self.ws.sock.settimeout(0.03)
            while time.monotonic() < deadline:
                try:
                    msg = json.loads(self.ws.recv_text())
                except socket.timeout:
                    continue
                if "method" in msg:
                    self.events.append(msg)
        finally:
            self.ws.sock.settimeout(old_timeout)

    def close(self) -> None:
        self.ws.close()


def _edge_candidates() -> list[Path]:
    roots = [os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")]
    rows: list[Path] = []
    for root in roots:
        if root:
            rows.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    return rows


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _json_get(url: str, timeout: float = 2.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_target(port: int, base_url: str, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            targets = _json_get(f"http://127.0.0.1:{port}/json/list")
            pages = [x for x in targets if x.get("type") == "page" and x.get("webSocketDebuggerUrl")]
            preferred = [x for x in pages if str(x.get("url") or "").startswith(base_url.rstrip("/"))]
            if preferred:
                return preferred[0]
            if pages:
                return pages[0]
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise TimeoutError(f"Edge DevTools target unavailable on port {port}: {last_error}")


def _wait_js(cdp: CDP, expression: str, timeout: float = 30.0, interval: float = 0.2) -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = cdp.evaluate(expression, timeout=min(5.0, max(1.0, deadline - time.monotonic())))
            if last:
                return last
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"browser condition did not become true: {expression[:160]}; last={last!r}")


def _screenshot(cdp: CDP, path: Path) -> None:
    try:
        data = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}, timeout=15.0).get("data")
        if data:
            path.write_bytes(base64.b64decode(data))
    except Exception:
        pass


def _terminate_profile_processes(profile: Path) -> list[int]:
    """Best-effort Windows cleanup limited to this probe's Edge profile."""
    killed: list[int] = []
    if os.name != "nt":
        return killed
    token = str(profile).lower()
    try:
        # PowerShell does not use backslash as a string escape.  JSON quoting a
        # Windows path would therefore turn each \\ into two literal slashes and
        # fail to match Edge's real command line.  Use a single-quoted PowerShell
        # literal and escape only PowerShell's own single quote.
        token_ps = token.replace("'", "''")
        cmd = [
            "powershell.exe", "-NoProfile", "-Command",
            "$t='" + token_ps + "'; Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($t) } | "
            "ForEach-Object { $_.ProcessId }",
        ]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in (run.stdout or "").splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            try:
                subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                killed.append(pid)
            except Exception:
                pass
    except Exception:
        pass
    return killed



def _http_bytes(url: str, timeout: float = 4.0) -> bytes:
    req = urllib.request.Request(url, headers={"Cache-Control":"no-cache, no-store", "Pragma":"no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _http_json(url: str, timeout: float = 4.0) -> Any:
    return json.loads(_http_bytes(url, timeout=timeout).decode("utf-8"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _trust_revision(trust: dict[str, Any] | None) -> tuple[int, int]:
    trust = dict(trust or {})
    stamp_raw = str(trust.get("evaluated_at") or "").strip()
    stamp = 0
    if stamp_raw:
        try:
            parsed = datetime.fromisoformat(stamp_raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            stamp = int(parsed.timestamp() * 1_000_000)
        except Exception:
            stamp = 0
    try:
        seq = int(trust.get("sequence_us") or 0)
    except Exception:
        seq = 0
    return stamp, seq


def _newest_trust(*trusts: Any) -> dict[str, Any]:
    rows = [dict(x or {}) for x in trusts if isinstance(x, dict)]
    return max(rows, key=_trust_revision) if rows else {}


def _candidate_evidence_complete(row: dict[str, Any]) -> bool:
    def txt(key: str) -> str:
        value = row.get(key)
        return str(value or "").strip().upper()
    generated = row.get("generated_at") or row.get("decision_generated_at") or row.get("created_at") or row.get("observed_at") or row.get("last_seen_at")
    if generated:
        try:
            parsed = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed.timestamp() > time.time() + 5:
                return False
        except Exception:
            return False
    readiness = txt("rank_readiness") or txt("evidence_readiness")
    scoring = txt("rank_scoring_state") or txt("scoring_state")
    freshness = txt("feature_freshness") or txt("freshness_state") or txt("evidence_freshness") or txt("price_freshness")
    snapshot = txt("feature_snapshot_state") or txt("snapshot_state") or txt("evidence_snapshot_state")
    combined = f"{readiness} {scoring} {freshness} {snapshot}"
    if any(token in combined for token in ("PARTIAL","INCOMPLETE","MISSING","UNKNOWN","STALE","INVALID","BLOCK")):
        return False
    for key in ("rank_missing_inputs", "rank_gate_failures", "rank_veto_reasons"):
        if isinstance(row.get(key), list) and any(row.get(key) or []):
            return False
    return readiness == "READY" and scoring == "NORMAL"


def _valid_past_timestamp(value: Any, tolerance_seconds: float = 5.0) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() <= time.time() + float(tolerance_seconds)
    except Exception:
        return False


def _money_num(text_value: str) -> float | None:
    raw = str(text_value or "").replace("₹", "").replace(",", "").strip()
    if not raw or raw in {"—", "-"}:
        return None
    try:
        value = float(raw)
    except Exception:
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _same_num(a: Any, b: Any, tol: float = 0.011) -> bool:
    try:
        x, y = float(a), float(b)
    except Exception:
        return False
    return abs(x-y) <= tol


def _find_decision(snapshot: dict[str, Any], desk: str) -> dict[str, Any]:
    desks = snapshot.get("desk_decisions") or {}
    node = desks.get(desk) if isinstance(desks, dict) else None
    if isinstance(node, dict) and isinstance(node.get("decision"), dict):
        return dict(node.get("decision") or {})
    if isinstance(snapshot.get("decision"), dict):
        return dict(snapshot.get("decision") or {})
    return {}


def _extract_model_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    found: list[dict[str, Any]] = []
    seen: set[int] = set()
    def walk(value: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            oid=id(value)
            if oid in seen: return
            seen.add(oid)
            if any(k in value for k in ("decision_id","source_decision_id","signal_id")) and any(k in value for k in ("entry","entry_price","original_entry","target","original_target","stop","original_stop","stop_price")):
                found.append(value)
            for v in value.values(): walk(v, depth+1)
        elif isinstance(value, list):
            for v in value[:2000]: walk(v, depth+1)
    walk(payload)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="Exact installed Project Laddu customer-vertical acceptance R3")
    ap.add_argument("--base-url", default="http://127.0.0.1:8086")
    ap.add_argument("--output", required=True)
    ap.add_argument("--install-dir", default=os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "ProjectLaddu"))
    ap.add_argument("--expected-version", default="v131.0.0")
    ap.add_argument("--expected-build", default="production-usability-r8-pl15-8086")
    # PL13 final acceptance has no empty-product mode.  Keep the historical
    # switches as command-line compatibility tokens, but their defaults are
    # deliberately true and there is no opt-out flag in the release runner.
    ap.add_argument("--require-market-open", action="store_true", default=True, help="Mandatory PL13 gate.")
    ap.add_argument("--require-full-sweeps", action="store_true", default=True, help="Mandatory PL13 gate.")
    ap.add_argument("--require-actionable", action="store_true", default=True, help="Mandatory PL13 gate.")
    ap.add_argument("--require-settlement", action="store_true", default=True, help="Mandatory PL13 gate.")
    ap.add_argument("--track-lifecycle", action="store_true", default=True, help="Mandatory: persist one exact decision through Actionable -> Paper -> Settlement -> After -> restart.")
    ap.add_argument("--tracker", default="", help="Persistent same-decision tracker JSON path.")
    ap.add_argument("--preferred-tracker-mode", default="intraday", choices=("intraday","delivery"))
    ap.add_argument("--verify-restart-before-boot-id", default="", help="After an actual service restart, require a different process boot id and the same settlement/After evidence.")
    ap.add_argument("--wait-seconds", type=int, default=180)
    args = ap.parse_args()

    base=args.base_url.rstrip("/")
    out=Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    shots=out.parent/"installed-customer-vertical-r3-screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]]=[]
    observations: dict[str, Any]={}
    result={
        "ok":False,
        "state":"FAILED",
        "authority":"EXACT_INSTALLED_CUSTOMER_VERTICAL_R3",
        "version":"installed-customer-vertical-3.0.0",
        "started_at":utcnow(),
        "base_url":base,
        "install_dir":args.install_dir,
        "expected_version":args.expected_version,
        "expected_build":args.expected_build,
        "checks":checks,
        "observations":observations,
        "screenshots":[],
        "console_errors":[],
        "request_failures":[],
        "market_evidence":{},
        "lifecycle_evidence":{},
    }
    cdp=None; edge_proc=None; profile=None; console_errors=[]; request_failures=[]

    def add(name: str, ok: bool, detail: Any=None) -> None:
        checks.append(check(name,ok,detail))
    def shot(name: str) -> None:
        if cdp is None: return
        path=shots/name; _screenshot(cdp,path)
        if path.exists(): result["screenshots"].append(str(path))

    try:
        ready=_http_json(base+"/api/ready",timeout=5)
        identity=_http_json(base+"/api/frontend-identity",timeout=8)

        # /api/ready proves the HTTP owner, not that background materializers
        # and supervised scanners have completed their bounded cold start.  A
        # fresh install must give those authorities time to publish real truth;
        # accepting NOT_STARTED/WARMING immediately is a race, while masking it
        # forever would be unsafe.  Poll only authoritative installed endpoints
        # and fail closed when the caller's bounded deadline expires.
        startup_deadline=time.monotonic()+max(30,int(args.wait_seconds))
        startup_samples=[]
        live={}; workspace={}; startup_performance={}; startup_model={}
        cadence_allowed={"RUNNING","SLEEPING","CONTINUING","DEGRADED","FAILED","BLOCKED","READY"}
        while True:
            try:
                live=_http_json(base+"/api/trader-live-state",timeout=5)
                workspace=_http_json(base+"/api/trader-workspace?mode=all",timeout=8)
                startup_performance=_http_json(base+"/api/performance?mode=all",timeout=8)
                startup_model=_http_json(base+"/api/model-portfolio?mode=all&detail=core",timeout=8)
                performance_ready=bool(startup_performance.get("canonical_lifecycle") or (startup_performance.get("performance_evidence") or {}).get("signal_accuracy")) and bool(startup_performance.get("model_paper_performance") or (startup_performance.get("performance_evidence") or {}).get("model_paper_performance"))
                cadence=dict((workspace.get("trust") or {}).get("scanner_cadence") or {})
                cadence_states={desk:str((cadence.get(desk) or {}).get("state") or "UNKNOWN").upper() for desk in ("intraday_scanner","delivery_scanner")}
                scanners_ready=all(state_name in cadence_allowed for state_name in cadence_states.values())
                startup_samples.append({"at":utcnow(),"performance_state":startup_performance.get("state"),"performance_ready":performance_ready,"scanner_states":cadence_states,"scanners_ready":scanners_ready})
                if performance_ready and scanners_ready:
                    break
            except Exception as exc:
                startup_samples.append({"at":utcnow(),"error":str(exc)[:400]})
            if time.monotonic()>=startup_deadline:
                break
            time.sleep(2.0)
        observations["startup_convergence"]={"deadline_seconds":max(30,int(args.wait_seconds)),"samples":startup_samples[-30:],"performance":startup_performance,"model_state":startup_model.get("state")}
        observations["ready"]=ready
        observations["frontend_identity"]=identity
        observations["live_state_initial"]=live
        observations["workspace_initial"]={k:workspace.get(k) for k in ("ok","contract_version","server_time","as_of","market_open","market_state","coverage","trust","route_elapsed_ms")}

        add("backend_exact_version", ready.get("version")==args.expected_version, ready)
        add("frontend_identity_endpoint_ok", identity.get("ok") is True and not (identity.get("mismatches") or []), identity)
        add("frontend_exact_version", identity.get("version")==args.expected_version and identity.get("manifest_version")==args.expected_version, identity)
        add("frontend_exact_build_marker", identity.get("build_marker")==args.expected_build, identity.get("build_marker"))
        add("workspace_contract_v15", str(workspace.get("contract_version") or "").startswith("trader-workspace-1.5.0"), workspace.get("contract_version"))
        add("live_truth_contract", str(live.get("contract_version") or "").startswith("trader-live-state-1."), live.get("contract_version"))
        initial_trust=dict(live.get("trust") or {})
        add(
            "runtime_trust_allows_new_decision_admission",
            initial_trust.get("state")=="TRUSTED" and initial_trust.get("decision_admission_allowed") is True,
            initial_trust,
        )

        # Served bytes must match the identity endpoint, not merely files on disk.
        asset_rows=[]; served_ok=True
        declared=dict(identity.get("declared_assets") or {})
        actual=dict(identity.get("assets") or {})
        for rel, expected_hash in declared.items():
            try:
                body=_http_bytes(base+"/"+str(rel).lstrip("/"),timeout=8)
                digest=_sha256_bytes(body)
                ok=digest==str(expected_hash).lower()==str(actual.get(rel) or "").lower()
            except Exception as exc:
                digest=""; ok=False; asset_rows.append({"asset":rel,"ok":False,"error":str(exc)[:300]}); served_ok=False; continue
            asset_rows.append({"asset":rel,"ok":ok,"served_sha256":digest,"declared_sha256":expected_hash})
            served_ok=served_ok and ok
        add("served_frontend_asset_hash_binding", served_ok and bool(asset_rows), asset_rows)

        install=Path(args.install_dir)
        if install.exists():
            disk_rows=[]; disk_ok=True
            for rel, expected_hash in declared.items():
                p=install/"frontend"/rel
                if not p.is_file():
                    disk_rows.append({"asset":rel,"ok":False,"reason":"missing"}); disk_ok=False; continue
                digest=hashlib.sha256(p.read_bytes()).hexdigest(); ok=digest==str(expected_hash).lower()
                disk_rows.append({"asset":rel,"ok":ok,"sha256":digest}); disk_ok=disk_ok and ok
            add("installed_frontend_disk_hash_binding", disk_ok and bool(disk_rows), disk_rows)
        else:
            add("installed_frontend_disk_hash_binding", False, f"install dir missing: {install}")

        # Numeric coverage is the only authority for full-universe rank.
        coverage=workspace.get("coverage") or {}
        coverage_rows={}
        coverage_ok=True
        for desk in ("delivery","intraday"):
            row=dict(coverage.get(desk) or {})
            processed=row.get("processed"); total=row.get("total"); complete=row.get("complete") is True
            try:
                numeric_complete=float(total)>0 and float(processed)>=float(total)
            except Exception:
                numeric_complete=False
            expected_scope="FULL_UNIVERSE" if numeric_complete else "EVALUATED_SUBSET_ONLY"
            ok=complete==numeric_complete and str(row.get("ranking_scope") or "")==expected_scope
            coverage_rows[desk]={**row,"expected_complete":numeric_complete,"expected_scope":expected_scope,"ok":ok}
            coverage_ok=coverage_ok and ok
        add("coverage_rank_scope_truth",coverage_ok,coverage_rows)

        edge=next((p for p in _edge_candidates() if p.exists()),None)
        if edge is None: raise FileNotFoundError("Microsoft Edge executable not found")
        port=_free_local_port(); profile=out.parent/"edge-r3-profile"
        if profile.exists(): shutil.rmtree(profile,ignore_errors=True)
        profile.mkdir(parents=True,exist_ok=True)
        stdout=open(out.parent/"edge-r3.stdout.log","wb"); stderr=open(out.parent/"edge-r3.stderr.log","wb")
        cmd=[str(edge),"--headless=new","--disable-gpu","--disable-background-networking","--disable-component-update","--no-first-run","--no-default-browser-check",f"--remote-debugging-port={port}",f"--user-data-dir={profile}","--window-size=1366,768",base+"/#workspace"]
        edge_proc=subprocess.Popen(cmd,stdout=stdout,stderr=stderr,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        target=_wait_target(port,base,timeout=25); cdp=CDP(str(target["webSocketDebuggerUrl"]))
        for method in ("Page.enable","Runtime.enable","Network.enable","Log.enable"): cdp.call(method)
        cdp.call("Emulation.setDeviceMetricsOverride",{"width":1366,"height":768,"deviceScaleFactor":1,"mobile":False})
        _wait_js(cdp,"Boolean(document.querySelector('[data-page-panel=\"workspace\"]')?.offsetParent && document.querySelector('#workspaceAsOf') && !/Awaiting snapshot/i.test(document.querySelector('#workspaceAsOf').innerText||''))",30)
        time.sleep(1.8)

        browser_identity=cdp.evaluate("""(() => ({version:(document.querySelector('#versionPill')?.innerText||'').trim(), buildVersion:document.documentElement.dataset.buildVersion||'', buildMarker:document.documentElement.dataset.buildMarker||'', frontendOwner:document.documentElement.dataset.frontendOwner||'', notice:(document.querySelector('#globalNotice')?.innerText||'').trim()}))()""") or {}
        add("browser_dom_build_marker",browser_identity.get("buildMarker")==args.expected_build,browser_identity)
        add("browser_dom_version_marker","v131" in str(browser_identity.get("version") or "") and "R8" in str(browser_identity.get("version") or "") and "8086" in str(browser_identity.get("version") or "") and "IDENTITY FAIL" not in str(browser_identity.get("version") or ""),browser_identity)

        # Fetch latest endpoints from inside the exact browser context, then compare DOM to newest trust revision.
        trust_sync=[]
        browser_endpoints={}; b_live={}; b_ws={}; b_identity={}; newest={}; dom={}; expected_trust="WARMING"; dom_trust=""
        trust_deadline=time.monotonic()+8.0
        while True:
            browser_endpoints=cdp.evaluate("""Promise.all([
              fetch('/api/trader-live-state',{cache:'no-store'}).then(r=>r.json()),
              fetch('/api/trader-workspace?mode=all',{cache:'no-store'}).then(r=>r.json()),
              fetch('/api/frontend-identity',{cache:'no-store'}).then(r=>r.json())
            ]).then(([live,workspace,identity])=>({live,workspace,identity}))""") or {}
            b_live=dict(browser_endpoints.get("live") or {}); b_ws=dict(browser_endpoints.get("workspace") or {}); b_identity=dict(browser_endpoints.get("identity") or {})
            newest=_newest_trust(b_live.get("trust"),b_ws.get("trust"))
            dom=cdp.evaluate("""(() => ({trust:(document.querySelector('#trustState')?.innerText||'').trim(),reason:(document.querySelector('#trustReason')?.innerText||'').trim(),market:(document.querySelector('#globalMarketState')?.innerText||'').trim(),live:(document.querySelector('#workspaceLiveState')?.innerText||'').trim(),watchMeta:(document.querySelector('#watchNextMeta')?.innerText||'').trim(),actionCount:document.querySelectorAll('#topEntriesRows tr.actionable-row').length,watchCount:document.querySelectorAll('#watchNextRows tr').length,outcomesHidden:document.querySelector('#recentOutcomesPanel')?.hidden===true,actionPanelHeight:document.querySelector('#actionablePanel')?.getBoundingClientRect().height||0,docWidth:document.documentElement.scrollWidth,viewport:window.innerWidth}))()""") or {}
            expected_trust=str(newest.get("state") or "WARMING").replace("_"," ").upper()
            dom_trust=str(dom.get("trust") or "").upper()
            trust_sync.append({"at":utcnow(),"dom":dom_trust,"expected":expected_trust,"live_revision":_trust_revision(b_live.get('trust')),"workspace_revision":_trust_revision(b_ws.get('trust'))})
            if dom_trust==expected_trust or time.monotonic()>=trust_deadline:
                break
            time.sleep(.4)
        add("browser_trust_matches_newest_runtime_authority",dom_trust==expected_trust,{"dom":dom,"newest":newest,"live_revision":_trust_revision(b_live.get('trust')),"workspace_revision":_trust_revision(b_ws.get('trust')),"synchronization":trust_sync})
        expected_market="MARKET LIVE" if b_live.get("market_open") is True else "MARKET CLOSED"
        add("browser_market_state_matches_runtime",expected_market in str(dom.get("market") or "").upper(),{"dom":dom.get("market"),"runtime":b_live.get("market_state")})
        add("no_live_validation_after_market_close", b_live.get("market_open") is True or "LIVE VALIDATION" not in str(cdp.evaluate("document.querySelector('#watchNextRows')?.innerText||''") or "").upper(), cdp.evaluate("document.querySelector('#watchNextRows')?.innerText||''"))

        # Cross-check Watch rows independently against endpoint evidence and coverage.
        dom_watch=cdp.evaluate("""(() => [...document.querySelectorAll('#watchNextRows tr')].map(tr=>{const t=[...tr.querySelectorAll('td')].map(td=>(td.innerText||'').trim());const b=tr.querySelector('[data-open-stock]');return {symbol:b?.dataset.openStock||'',mode:b?.dataset.mode||'',cells:t};}))()""") or []
        candidates=[dict(x or {}) for x in (b_ws.get("candidates") or []) if isinstance(x,dict)]
        cand_map={(str(x.get("symbol") or x.get("trading_symbol") or "").upper(),str(x.get("mode") or "delivery").lower()):x for x in candidates}
        watch_checks=[]; watch_ok=True
        for row in dom_watch:
            symbol=str(row.get("symbol") or "").upper(); desk=str(row.get("mode") or "delivery").lower(); cells=row.get("cells") or []
            if not symbol: continue
            source=cand_map.get((symbol,desk),{})
            cov=dict((b_ws.get("coverage") or {}).get(desk) or {})
            full=cov.get("complete") is True
            rank=(cells[0] if len(cells)>0 else "").strip(); score=(cells[4] if len(cells)>4 else "").strip(); stage=(cells[8] if len(cells)>8 else "").strip().upper()
            evidence_complete=_candidate_evidence_complete(source)
            ok_rank=(rank.isdigit() if full else not rank.isdigit())
            ok_score=(score not in {"—","","Unavailable"}) if evidence_complete else score in {"—","","Unavailable"}
            ok_stage=not (b_live.get("market_open") is False and stage=="LIVE VALIDATION")
            ok=ok_rank and ok_score and ok_stage
            watch_checks.append({"symbol":symbol,"mode":desk,"rank":rank,"score":score,"stage":stage,"full_universe":full,"evidence_complete":evidence_complete,"ok":ok})
            watch_ok=watch_ok and ok
        add("browser_watch_scope_evidence_semantics",watch_ok,watch_checks)
        watch_age_rows=[]; watch_age_ok=True
        for row in dom_watch:
            cells=row.get("cells") or []
            age=(cells[7] if len(cells)>7 else "").strip()
            ok=bool(age) and age not in {"—","-","Not timestamped","Missing signal time"}
            watch_age_rows.append({"symbol":row.get("symbol"),"mode":row.get("mode"),"age":age,"ok":ok})
            watch_age_ok=watch_age_ok and ok
        add("watch_rows_have_authoritative_age_semantics",watch_age_ok,watch_age_rows)

        # Empty panels must collapse rather than dominate the customer viewport.
        actionable_count=int(dom.get("actionCount") or 0)
        if actionable_count==0:
            add("empty_actionable_panel_collapsed",float(dom.get("actionPanelHeight") or 9999)<190,dom)
        else:
            add("empty_actionable_panel_collapsed",True,{"not_applicable":True,"actionable":actionable_count})
        add("browser_no_horizontal_viewport_overflow",float(dom.get("docWidth") or 99999)<=float(dom.get("viewport") or 0)+2,dom)
        if not any((b_ws.get("final_signals") or [])):
            # Outcome may exist independently; only assert hidden when its table is actually empty.
            outcome_text=str(cdp.evaluate("document.querySelector('#workspaceOutcomeRows')?.innerText||''") or "").strip()
            empty_outcome=(not outcome_text) or "NO SETTLED" in outcome_text.upper() or "UNAVAILABLE" in outcome_text.upper()
            add("empty_outcomes_panel_collapsed",(not empty_outcome) or bool(dom.get("outcomesHidden")),{"empty":empty_outcome,"hidden":dom.get("outcomesHidden"),"text":outcome_text[:300]})

        # Multi-viewport installed browser geometry.
        geometry_rows=[]; geometry_ok=True
        for width,height in ((1366,768),(1600,900),(1920,1080)):
            cdp.call("Emulation.setDeviceMetricsOverride",{"width":width,"height":height,"deviceScaleFactor":1,"mobile":False}); time.sleep(.15)
            g=cdp.evaluate("""(() => {const main=document.querySelector('.main-content')||document.querySelector('main');const ws=document.querySelector('[data-page-panel="workspace"]');const r=(ws||main)?.getBoundingClientRect();return {innerWidth:innerWidth,scrollWidth:document.documentElement.scrollWidth,left:r?.left||0,right:r?.right||0,width:r?.width||0,visible:!!(r&&r.width&&r.height)};})()""") or {}
            ok=bool(g.get("visible")) and float(g.get("scrollWidth") or 99999)<=width+2 and float(g.get("width") or 0)>=width*.78 and float(g.get("right") or 0)<=width+2
            geometry_rows.append({"viewport":[width,height],**g,"ok":ok}); geometry_ok=geometry_ok and ok
            shot(f"workspace-{width}x{height}.png")
        add("installed_browser_full_width_multi_viewport",geometry_ok,geometry_rows)
        cdp.call("Emulation.setDeviceMetricsOverride",{"width":1366,"height":768,"deviceScaleFactor":1,"mobile":False})

        # Exact installed R8 customer workflow: every primary destination must
        # activate in one application and load its own authority surface.
        page_specs = (
            ("workspace", "#actionablePanel"),
            ("report", "#stockReportHeading"),
            ("model-paper", "#modelPaperState"),
            ("accuracy", "#accuracyStats"),
            ("research", "#researchCandidateHistoryRows"),
            ("system", "#systemState"),
        )
        page_rows=[]; pages_ok=True
        for page, selector in page_specs:
            cdp.evaluate(f"document.querySelector('[data-page={json.dumps(page)}]')?.click()")
            try:
                _wait_js(cdp, f"Boolean(document.querySelector('[data-page-panel={json.dumps(page)}].active') && document.querySelector({json.dumps(selector)}))", 15)
                page_ok=True
            except Exception as exc:
                page_ok=False
                page_rows.append({"page":page,"selector":selector,"ok":False,"error":str(exc)[:400]})
            if page_ok:
                page_rows.append({"page":page,"selector":selector,"ok":True})
            pages_ok=pages_ok and page_ok
        add("all_customer_navigation_destinations_load",pages_ok,page_rows)

        workflow_data=cdp.evaluate("""Promise.all([
          fetch('/api/model-portfolio?mode=all&detail=core',{cache:'no-store'}).then(r=>r.json()),
          fetch('/api/performance?mode=all',{cache:'no-store'}).then(r=>r.json())
        ]).then(([model,performance])=>({model,performance}))""") or {}
        workflow_model=dict(workflow_data.get("model") or {}); workflow_performance=dict(workflow_data.get("performance") or {})
        research_perf=dict(workflow_model.get("research_performance") or {})
        add("research_history_and_performance_authority_load", isinstance(workflow_model.get("research"),list) and research_perf.get("authority")=="PERSISTENT_RESEARCH_COUNTERFACTUAL_ONLY" and research_perf.get("included_in_final_performance") is False, {"research_count":len(workflow_model.get("research") or []),"performance":research_perf})
        add("final_accuracy_and_model_paper_authorities_load", bool(workflow_performance.get("canonical_lifecycle") or (workflow_performance.get("performance_evidence") or {}).get("signal_accuracy")) and bool(workflow_performance.get("model_paper_performance") or (workflow_performance.get("performance_evidence") or {}).get("model_paper_performance")), {"state":workflow_performance.get("state")})

        # A browser refresh must retain every currently persisted Research ID.
        research_before=sorted({str(row.get("research_candidate_id") or row.get("source_signal_id") or "") for row in (workflow_model.get("research") or []) if isinstance(row,dict) and str(row.get("research_candidate_id") or row.get("source_signal_id") or "")})
        cdp.call("Page.reload",{"ignoreCache":True})
        _wait_js(cdp,"Boolean(document.querySelector('[data-page-panel=\"system\"]')?.classList.contains('active') && document.querySelector('#versionPill')?.innerText.includes('R8'))",30)
        model_after_refresh=cdp.evaluate("fetch('/api/model-portfolio?mode=all&detail=core',{cache:'no-store'}).then(r=>r.json())") or {}
        research_after=sorted({str(row.get("research_candidate_id") or row.get("source_signal_id") or "") for row in (model_after_refresh.get("research") or []) if isinstance(row,dict) and str(row.get("research_candidate_id") or row.get("source_signal_id") or "")})
        add("persistent_research_survives_browser_refresh",bool(research_before) and set(research_before).issubset(set(research_after)),{"before":research_before,"after":research_after})

        # Direct Research and decision/Stock Intelligence routes must resolve.
        if research_before:
            focus_id=research_before[0]
            cdp.evaluate(f"location.hash='research?candidate='+encodeURIComponent({json.dumps(focus_id)})")
            try:
                _wait_js(cdp, f"Boolean(document.querySelector('[data-page-panel=\"research\"].active') && [...document.querySelectorAll('tr[data-research-candidate]')].some(row=>row.dataset.researchCandidate==={json.dumps(focus_id)} && row.getClientRects().length))", 20)
                direct_research_ok=True
            except Exception:
                direct_research_ok=False
            direct_research_dom=cdp.evaluate("""(() => ({active:document.querySelector('[data-page-panel="research"]')?.classList.contains('active')===true,page:(document.querySelector('#researchPageState')?.innerText||'').trim(),visible:[...document.querySelectorAll('tr[data-research-candidate]')].filter(row=>row.getClientRects().length).map(row=>row.dataset.researchCandidate),focused:[...document.querySelectorAll('tr.research-focused[data-research-candidate]')].map(row=>row.dataset.researchCandidate)}))()""") or {}
            add("direct_research_url_loads_exact_candidate",direct_research_ok,{"candidate":focus_id,"hash":cdp.evaluate("location.hash"),"dom":direct_research_dom})
        else:
            add("direct_research_url_loads_exact_candidate",False,"No persisted Research candidate exists to prove the installed direct route.")

        final_for_route=[dict(row or {}) for row in (b_ws.get("final_signals") or []) if isinstance(row,dict)]
        research_rows=[dict(row or {}) for row in (workflow_model.get("research") or []) if isinstance(row,dict)]
        candidate_rows=[dict(row or {}) for row in (b_ws.get("candidates") or []) if isinstance(row,dict)]
        route_source=(final_for_route or research_rows or candidate_rows)
        if route_source:
            route_row=route_source[0]; route_symbol=str(route_row.get("symbol") or route_row.get("trading_symbol") or "").upper(); route_mode=str(route_row.get("mode") or "delivery").lower(); route_decision=str(route_row.get("decision_id") or route_row.get("signal_id") or route_row.get("research_candidate_id") or "")
            cdp.evaluate(f"location.hash='report?symbol='+encodeURIComponent({json.dumps(route_symbol)})+'&mode='+encodeURIComponent({json.dumps(route_mode)})+'&decision='+encodeURIComponent({json.dumps(route_decision)})")
            try:
                _wait_js(cdp, f"Boolean(document.querySelector('[data-page-panel=\"report\"].active') && (document.querySelector('#reportTitle')?.innerText||'').trim()==={json.dumps(route_symbol)} && location.hash.includes('decision='))",20)
                direct_decision_ok=True
            except Exception:
                direct_decision_ok=False
            add("direct_decision_stock_intelligence_url_loads",direct_decision_ok,{"symbol":route_symbol,"mode":route_mode,"decision":route_decision,"hash":cdp.evaluate("location.hash")})
        else:
            add("direct_decision_stock_intelligence_url_loads",False,"No installed Final/Research candidate exists to prove the direct route.")

        # Installed scanner semantics: intentional sleep is coherent evidence,
        # never itself a failed scanner projection.
        cadence=dict((b_ws.get("trust") or {}).get("scanner_cadence") or {})
        cadence_rows=[]; cadence_ok=True; healthy_sleep_failure=False
        for desk in ("intraday_scanner","delivery_scanner"):
            row=dict(cadence.get(desk) or {}); state_name=str(row.get("state") or "").upper()
            allowed=state_name in {"RUNNING","SLEEPING","CONTINUING","READY"} and row.get("healthy") is True
            coherent=all(row.get(key) is not None for key in ("last_cycle_at","next_cycle_at","seconds_to_next","heartbeat_age_sec"))
            if state_name=="SLEEPING" and row.get("healthy") is True:
                healthy_sleep_failure = healthy_sleep_failure or "scanner failed" in str((b_ws.get("trust") or {}).get("reason") or "").lower()
            ok=allowed and coherent; cadence_rows.append({"desk":desk,"ok":ok,**row}); cadence_ok=cadence_ok and ok
        add("installed_scanner_lifecycle_is_coherent",cadence_ok,cadence_rows)
        add("healthy_scanner_sleep_is_not_a_failure",not healthy_sleep_failure,{"trust":b_ws.get("trust"),"cadence":cadence_rows})

        cdp.evaluate("document.querySelector('[data-page=\"workspace\"]')?.click()")
        _wait_js(cdp,"Boolean(document.querySelector('[data-page-panel=\"workspace\"].active'))",15)

        # Canonical decision parity if an actionable signal exists now.
        final=[dict(x or {}) for x in (b_ws.get("final_signals") or []) if isinstance(x,dict)]
        result["market_evidence"]={"market_open":b_live.get("market_open"),"coverage":b_ws.get("coverage"),"actionable_count":len(final),"workspace_as_of":b_ws.get("as_of"),"live_server_time":b_live.get("server_time")}
        if final:
            f=final[0]; decision_id=str(f.get("decision_id") or f.get("signal_id") or ""); symbol=str(f.get("symbol") or f.get("trading_symbol") or "").upper(); desk=str(f.get("mode") or "delivery").lower()
            time_semantics=dict(f.get("time_semantics") or {})
            generated=f.get("generated_at") or f.get("decision_generated_at") or time_semantics.get("generated_at")
            try:
                signal_age=float(f.get("signal_age_seconds"))
            except (TypeError,ValueError):
                signal_age=-1
            holding=str(f.get("holding_period") or f.get("target_window") or time_semantics.get("holding_period") or "").strip()
            complete_fields={
                "decision_id":bool(decision_id),
                "final_authority":bool(str(f.get("final_signal_authority") or "").strip()),
                "ltp":_money_num(str(f.get("display_price") if f.get("display_price") is not None else f.get("current_price"))) is not None,
                "change_abs":f.get("display_change_abs") is not None,
                "change_pct":f.get("display_change_pct") is not None,
                "entry":f.get("display_entry",f.get("entry")) is not None,
                "target":f.get("display_target",f.get("target")) is not None,
                "stop":f.get("display_stop",f.get("stop")) is not None,
                "rr":f.get("display_rr",f.get("rr")) is not None,
                "generated_at":bool(generated and _valid_past_timestamp(generated)),
                "signal_age":signal_age>=0,
                "holding_period":bool(holding),
                "lifecycle_state":bool(str(f.get("display_stage") or f.get("lifecycle_state") or f.get("status") or "").strip()),
            }
            add("actionable_row_has_complete_governed_fields",all(complete_fields.values()),{"fields":complete_fields,"decision_id":decision_id,"time_semantics":time_semantics})
            row=cdp.evaluate(f"""(() => {{const tr=[...document.querySelectorAll('#topEntriesRows tr.actionable-row')].find(x=>x.dataset.decisionId==={json.dumps(decision_id)});if(!tr)return null;const c=[...tr.querySelectorAll('td')].map(td=>(td.innerText||'').trim());return {{decisionId:tr.dataset.decisionId,authority:tr.dataset.finalAuthority,symbol:tr.dataset.openStock,mode:tr.dataset.mode,cells:c}};}})()""")
            add("actionable_dom_exact_decision_identity",bool(row) and row.get("decisionId")==decision_id and row.get("symbol")==symbol and row.get("mode")==desk,{"api":f,"dom":row})
            if row:
                cells=row.get("cells") or []
                dom_entry=_money_num(cells[7] if len(cells)>7 else ""); dom_target=_money_num(cells[8] if len(cells)>8 else ""); dom_stop=_money_num(cells[9] if len(cells)>9 else "")
                api_entry=f.get("display_entry",f.get("entry")); api_target=f.get("display_target",f.get("target")); api_stop=f.get("display_stop",f.get("original_stop",f.get("stop")))
                add("actionable_frozen_geometry_parity",_same_num(dom_entry,api_entry) and _same_num(dom_target,api_target) and _same_num(dom_stop,api_stop),{"dom":[dom_entry,dom_target,dom_stop],"api":[api_entry,api_target,api_stop]})
            # Stock Intelligence must expose the same decision id.
            lookup=str(f.get("instrument_key") or symbol)
            snap=cdp.evaluate(f"fetch('/api/stock-snapshot?symbol='+encodeURIComponent({json.dumps(lookup)})+'&mode='+encodeURIComponent({json.dumps(desk)}),{{cache:'no-store'}}).then(r=>r.json())") or {}
            snap_dec=_find_decision(snap,desk); snap_id=str(snap_dec.get("decision_id") or snap_dec.get("signal_id") or "")
            add("stock_intelligence_same_decision_identity",snap_id==decision_id,{"decision_id":decision_id,"snapshot_id":snap_id,"symbol":symbol,"mode":desk})
            # Model Paper must either carry exact lineage or explicitly not yet be opened.
            model=cdp.evaluate("fetch('/api/model-portfolio?mode=all&detail=core',{cache:'no-store'}).then(r=>r.json())") or {}
            model_rows=_extract_model_rows(model)
            matches=[x for x in model_rows if str(x.get("decision_id") or x.get("source_decision_id") or x.get("signal_id") or "")==decision_id]
            stage=str(f.get("display_stage") or f.get("status") or "").upper()
            open_stage=any(t in stage for t in ("OPEN","ACTIVE","SIGNAL_OPEN"))
            add("model_paper_exact_lineage_for_open_decision",(not open_stage) or bool(matches),{"decision_id":decision_id,"stage":stage,"matches":matches[:3]})
            result["lifecycle_evidence"].update({"decision_id":decision_id,"symbol":symbol,"mode":desk,"stage":stage,"model_matches":len(matches)})
        else:
            detail={"market_open":b_live.get("market_open"),"coverage":b_ws.get("coverage"),"reason":"No canonical actionable decision exists in the current installed runtime."}
            add("real_actionable_signal_observed",not args.require_actionable,detail)

        # Market/full sweep requirements are explicit and never silently waived.
        if args.require_market_open:
            add("market_open_required",b_live.get("market_open") is True,b_live)
        else:
            add("market_state_observed",b_live.get("market_open") in (True,False),b_live.get("market_state"))
        if args.require_full_sweeps:
            cov=b_ws.get("coverage") or {}; full=all(dict(cov.get(d) or {}).get("complete") is True for d in ("delivery","intraday"))
            add("full_delivery_intraday_sweeps_required",full,cov)

        # Settlement/Outcome/After are only accepted from real persisted evidence.
        if args.require_settlement:
            perf=cdp.evaluate("fetch('/api/performance?mode=all',{cache:'no-store'}).then(r=>r.json())") or {}
            lifecycle=perf.get("canonical_lifecycle") or (perf.get("performance_evidence") or {}).get("signal_accuracy") or {}
            records=[x for x in (lifecycle.get("records") or []) if isinstance(x,dict)]
            eligible=[x for x in records if x.get("accuracy_eligible") is True or x.get("performance_eligible") is True]
            has_result=any(str(x.get("exit_reason") or x.get("result") or x.get("display_result") or "").strip() for x in eligible)
            has_after=any(str(x.get("after") or x.get("after_state") or x.get("follow_through_state") or x.get("post_exit_state") or "").strip() for x in eligible)
            add("real_settlement_result_observed",has_result,{"eligible":len(eligible),"sample":eligible[:3]})
            add("real_post_exit_after_observed",has_after,{"eligible":len(eligible),"sample":eligible[:3]})
            result["lifecycle_evidence"]["eligible_settlements"]=len(eligible)

        # Persistent exact-decision vertical tracker. Unlike a generic historical
        # settlement count, this can only advance using the same decision_id that
        # was first observed as actionable on this exact build.
        if args.track_lifecycle:
            tracker_path = Path(args.tracker) if args.tracker else out.parent / "EXACT_VERTICAL_TRACKER.json"
            tracker_before = load_vertical_tracker(tracker_path)
            model_for_tracker = cdp.evaluate("fetch('/api/model-portfolio?mode=all&detail=core',{cache:'no-store'}).then(r=>r.json())") or {}
            performance_for_tracker = cdp.evaluate("fetch('/api/performance?mode=all',{cache:'no-store'}).then(r=>r.json())") or {}
            restart_proof = None
            if args.verify_restart_before_boot_id:
                tracked_id = str(tracker_before.get("decision_id") or "")
                persisted = any(tracked_id and tracked_id in tracker_ids(row) for row in tracker_lifecycle_records(performance_for_tracker))
                restart_proof = {
                    "before_boot_id": args.verify_restart_before_boot_id,
                    "after_boot_id": str(ready.get("process_boot_id") or ""),
                    "same_settlement_persisted": persisted,
                }
            tracker_after, tracker_errors = update_vertical_tracker(
                tracker_before, live=b_live, workspace=b_ws, model=model_for_tracker, performance=performance_for_tracker,
                expected_version=args.expected_version, expected_build=args.expected_build,
                preferred_mode=args.preferred_tracker_mode, require_full_sweep=True, restart_proof=restart_proof,
            )
            save_vertical_tracker(tracker_path, tracker_after)
            result["tracker"] = {"path": str(tracker_path), "state": tracker_after, "errors": tracker_errors}
            add("same_decision_lifecycle_tracker_integrity", not tracker_errors, {"stage":tracker_after.get("stage"),"decision_id":tracker_after.get("decision_id"),"errors":tracker_errors})

        cdp.drain(.4)
    except Exception as exc:
        result["fatal_error"]=f"{type(exc).__name__}: {exc}"
    finally:
        if cdp is not None:
            try: cdp.drain(.1)
            except Exception: pass
            reqs={}
            for event in cdp.events:
                method=event.get("method"); params=event.get("params") or {}
                if method=="Runtime.exceptionThrown":
                    d=params.get("exceptionDetails") or {}; console_errors.append("pageerror: "+str(d.get("text") or d.get("exception") or d)[:2000])
                elif method=="Runtime.consoleAPICalled" and params.get("type")=="error":
                    args2=params.get("args") or []; console_errors.append(" ".join(str(a.get("value") if "value" in a else a.get("description") or "") for a in args2)[:2000])
                elif method=="Log.entryAdded":
                    e=params.get("entry") or {}
                    if e.get("level")=="error": console_errors.append(str(e.get("text") or e)[:2000])
                elif method=="Network.requestWillBeSent": reqs[str(params.get("requestId") or "")]=str((params.get("request") or {}).get("url") or "")
                elif method=="Network.loadingFailed": request_failures.append(f"{reqs.get(str(params.get('requestId') or ''),'')}: {params.get('errorText') or ''}"[:2500])
            try: cdp.close()
            except Exception: pass
        if edge_proc is not None:
            try: edge_proc.terminate(); edge_proc.wait(timeout=3)
            except Exception:
                try: edge_proc.kill(); edge_proc.wait(timeout=2)
                except Exception: pass
        if profile is not None:
            _terminate_profile_processes(profile); shutil.rmtree(profile,ignore_errors=True)

    meaningful=[x for x in request_failures if "ERR_ABORTED" not in x and "NS_BINDING_ABORTED" not in x]
    add("browser_console_clean",len(console_errors)==0,console_errors[:20])
    add("browser_network_failures_clean",len(meaningful)==0,meaningful[:20])
    result["console_errors"]=console_errors; result["request_failures"]=request_failures
    result["passed"]=sum(1 for x in checks if x["ok"]); result["failed"]=sum(1 for x in checks if not x["ok"])
    hard_ok=result["failed"]==0 and not result.get("fatal_error")
    tracker_state = ((result.get("tracker") or {}).get("state") or {}) if args.track_lifecycle else {}
    tracker_complete = str(tracker_state.get("stage") or "") == "RESTART_VERIFIED" and tracker_state.get("complete") is True
    market_pending = (not args.require_market_open and result.get("market_evidence",{}).get("market_open") is False)
    if args.track_lifecycle and hard_ok and not tracker_complete:
        result["state"]="SAME_DECISION_LIFECYCLE_PENDING"
        result["pending"]=True
        result["ok"]=False
    elif args.track_lifecycle and hard_ok and tracker_complete:
        result["state"]="FULL_EXACT_CUSTOMER_VERTICAL_PASSED"
        result["pending"]=False
        result["ok"]=True
    elif hard_ok and market_pending and not args.require_actionable and not args.require_settlement:
        result["state"]="INSTALLED_CLOSED_MARKET_PROOF_PASSED_LIVE_VERTICAL_PENDING"
        result["ok"]=True
    elif hard_ok:
        result["state"]="EXACT_INSTALLED_CUSTOMER_VERTICAL_PASSED"
        result["ok"]=True
    else:
        result["state"]="FAILED"
        result["ok"]=False
    result["completed_at"]=utcnow()
    out.write_text(json.dumps(result,indent=2,sort_keys=True),encoding="utf-8")
    sha=hashlib.sha256(out.read_bytes()).hexdigest(); (Path(str(out)+".sha256")).write_text(f"{sha}  {out.name}\n",encoding="ascii")
    print(json.dumps({"ok":result["ok"],"state":result["state"],"passed":result["passed"],"failed":result["failed"],"output":str(out)}))
    if result.get("pending") is True:
        return 3
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
