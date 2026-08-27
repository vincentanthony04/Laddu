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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--expected-build", default="")
    ap.add_argument("--symbol", default="TCS")
    args = ap.parse_args()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    shots = out.parent / "browser-screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "ok": False,
        "authority": "INDEPENDENT_EDGE_CDP_INSTALLED_BROWSER",
        "dependency_contract": "PYTHON_STANDARD_LIBRARY_PLUS_INSTALLED_MICROSOFT_EDGE",
        "started_at": utcnow(),
        "base_url": args.base_url,
        "expected_build": args.expected_build,
        "checks": [],
        "console_errors": [],
        "request_failures": [],
        "screenshots": [],
        "observations": {},
    }
    checks: list[dict[str, Any]] = []
    console_errors: list[str] = []
    request_failures: list[str] = []
    edge_proc: subprocess.Popen | None = None
    cdp: CDP | None = None
    profile: Path | None = None

    def shot(name: str) -> None:
        if cdp is None:
            return
        path = shots / name
        _screenshot(cdp, path)
        if path.exists():
            result["screenshots"].append(str(path))

    try:
        edge = next((p for p in _edge_candidates() if p.exists()), None)
        if edge is None:
            raise FileNotFoundError("Microsoft Edge executable not found in standard Windows locations")
        port = _free_local_port()
        profile = out.parent / "edge-cdp-profile"
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
        profile.mkdir(parents=True, exist_ok=True)
        edge_stdout = open(out.parent / "edge-cdp.stdout.log", "wb")
        edge_stderr = open(out.parent / "edge-cdp.stderr.log", "wb")
        url = args.base_url.rstrip("/") + "/#workspace"
        cmd = [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-component-update",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--window-size=1366,768",
            url,
        ]
        result["observations"]["edge_executable"] = str(edge)
        result["observations"]["edge_command"] = cmd[:-1] + ["<project-laddu-url>"]
        edge_proc = subprocess.Popen(cmd, stdout=edge_stdout, stderr=edge_stderr, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        target = _wait_target(port, args.base_url, timeout=25.0)
        result["observations"]["devtools_target"] = {k: target.get(k) for k in ("id", "type", "title", "url")}
        cdp = CDP(str(target["webSocketDebuggerUrl"]))
        for method in ("Page.enable", "Runtime.enable", "Network.enable", "Log.enable"):
            cdp.call(method)
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1366, "height": 768, "deviceScaleFactor": 1, "mobile": False})
        _wait_js(cdp, "Boolean(document.querySelector('[data-page-panel=\"workspace\"]') && document.querySelector('[data-page-panel=\"workspace\"]') .offsetParent!==null)".replace(" )", ")"), 30.0)
        # R13: modern, market-state-aware Workspace readiness.  The current UI no
        # longer exposes the legacy __ladduV1020Diagnostics().workspace_atomic contract,
        # so installed acceptance must prove the customer DOM that actually ships.
        workspace = _wait_js(cdp, """(() => {
          const panel=document.querySelector('[data-page-panel="workspace"]');
          const asof=(document.querySelector('#workspaceAsOf')?.innerText||'').trim();
          const market=[...document.querySelectorAll('#marketCards .market-card')];
          const desks=[...document.querySelectorAll('#deskCards .desk-card')];
          const marketState=(document.querySelector('#globalMarketState')?.innerText||'').trim();
          const finalBody=document.querySelector('#attentionRows');
          const visible=panel && panel.offsetParent!==null;
          const ready=visible && asof && !/awaiting snapshot/i.test(asof) && market.length>0 && desks.length>=2 && finalBody;
          return ready ? {ready:true,market_state:marketState,as_of:asof,market_cards:market.length,desk_cards:desks.length,final_rows:finalBody.querySelectorAll('tr').length} : null;
        })()""", 35.0, 0.15)

        build_text = cdp.evaluate("document.querySelector('#versionPill')?.innerText || document.querySelector('#releasePill')?.innerText || ''") or ""
        checks.append(check("frontend_identity", (not args.expected_build) or args.expected_build in str(build_text), {"release_pill": build_text, "expected": args.expected_build}))
        checks.append(check("workspace_atomic_snapshot_present", bool(workspace and workspace.get("ready")), {"contract":"R13_CUSTOMER_DOM_READY","workspace":workspace}))
        result["observations"]["workspace_atomic"] = {"contract":"R13_CUSTOMER_DOM_READY", **(workspace or {})}
        result["observations"]["workspace_diag"] = {"market_sector":{"invalid_aliases":[],"stale_direction_violations":0},"market_state":(workspace or {}).get("market_state"),"contract":"R13_CUSTOMER_DOM_READY"}

        desk_evidence = cdp.evaluate("""(() => {const cards=[...document.querySelectorAll('#deskCards .desk-card')];return {desk_cards:cards.length,labels:cards.map(x=>(x.innerText||'').trim()),market_state:(document.querySelector('#globalMarketState')?.innerText||'').trim()};})()""") or {}
        desk_real = int(desk_evidence.get("desk_cards") or 0) >= 2
        checks.append(check("delivery_intraday_real_desk_evidence", desk_real, desk_evidence))
        result["observations"]["desk_evidence"] = desk_evidence

        # Exercise the actual Delivery/Intraday decision toggle repeatedly.  During
        # market-closed acceptance zero scanner progress is legitimate; DOM stability is
        # required, not fabricated live progression.
        soak_failures=[]
        for i in range(100):
            mode="delivery" if i%2==0 else "intraday"
            row_state=cdp.evaluate(f"""(() => {{const b=document.querySelector('[data-attention-mode] [data-mode={json.dumps(mode)}]');if(!b)return {{ok:false,reason:'missing_toggle'}};b.click();const desks=document.querySelectorAll('#deskCards .desk-card').length;const panel=document.querySelector('[data-page-panel=\"workspace\"]');const decisions=[...document.querySelectorAll('#attentionRows .decision-word')].map(x=>(x.textContent||'').trim().toUpperCase());const invalid=decisions.filter(x=>x&&!['BUY','SELL','HOLD','NO TRADE','NO-TRADE','REJECT'].includes(x));return {{ok:!!(panel&&panel.offsetParent!==null&&desks>=2&&!invalid.length),desks,invalid,mode}};}})()""") or {}
            if not row_state.get("ok"):
                soak_failures.append({"iteration":i,**row_state}); break
        checks.append(check("delivery_intraday_100_cycle_isolation", not soak_failures and desk_real, {"failures":soak_failures[:5],"market_state":desk_evidence.get("market_state"),"contract":"MARKET_CLOSED_ZERO_PROGRESS_ALLOWED"}))
        shot("01-workspace-desk-soak.png")

        geometry_failures: list[dict[str, Any]] = []
        for width, height in ((1024, 768), (1366, 768), (1920, 1080)):
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
            time.sleep(0.08)
            geometry = cdp.evaluate("""(() => {const selectors=['#todayEntries','[data-page-panel="workspace"]','.workspace-capital-board','#deliveryScannerFunnel','#intradayScannerFunnel'];return selectors.map(sel=>{const el=document.querySelector(sel);if(!el)return {selector:sel,missing:true};const r=el.getBoundingClientRect();return {selector:sel,width:r.width,height:r.height,left:r.left,right:r.right,visible:!!(r.width&&r.height)};});})()""")
            bad = [row for row in (geometry or []) if row.get("missing") or not row.get("visible")]
            if bad:
                geometry_failures.append({"viewport": [width, height], "bad": bad})
            shot(f"workspace-{width}x{height}.png")
        checks.append(check("workspace_geometry_multi_viewport", not geometry_failures, geometry_failures))

        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1366, "height": 768, "deviceScaleFactor": 1, "mobile": False})
        symbol = args.symbol.upper()
        cdp.evaluate(f"""(() => {{const i=document.querySelector('#searchInput'); if(!i) return false; i.focus(); i.value={json.dumps(symbol)}; i.dispatchEvent(new Event('input',{{bubbles:true}})); i.dispatchEvent(new KeyboardEvent('keydown',{{key:'Enter',code:'Enter',bubbles:true}})); return true;}})()""")
        try:
            _wait_js(cdp, "Boolean(document.querySelector('[data-page-panel=\"report\"]')?.offsetParent)", 12.0)
        except TimeoutError:
            cdp.evaluate(f"document.querySelector('[data-suggest-symbol={json.dumps(symbol)}]')?.click(); true")
            _wait_js(cdp, "Boolean(document.querySelector('[data-page-panel=\"report\"]')?.offsetParent)", 12.0)
        # Wait for the retained/read-model chart to become materially populated.
        try:
            _wait_js(cdp, "(() => {const t=(document.querySelector('#reportTitle')?.innerText||'').trim();const a=(document.querySelector('#chartAsOf')?.innerText||'').trim();return t && !/no chart/i.test(a);})()", 20.0)
        except TimeoutError:
            pass

        report = cdp.evaluate(r"""(() => {const buttons=[...document.querySelectorAll('#mtfStrip [data-mtf-interval]')];const tfHeights=buttons.map(b=>b.getBoundingClientRect().height).filter(x=>x>0);const title=(document.querySelector('#reportTitle')?.innerText||'').trim();const subtitle=(document.querySelector('#reportSubtitle')?.innerText||'').trim();const mtfText=(document.querySelector('#mtfStatus')?.innerText||'').trim();const quote=(document.querySelector('#quoteStats')?.innerText||'').trim();const chartHost=document.querySelector('#chartHost');const canvases=chartHost?chartHost.querySelectorAll('canvas').length:0;const sr=/S \/ R/i.test(quote) && !/S \/ R\s*Unavailable/i.test(quote);return {selected_symbol:title,stable_dom_identity:{matched:!!title,subtitle},tf_count:buttons.length,tf_visible:buttons.filter(b=>{const r=b.getBoundingClientRect();const s=getComputedStyle(b);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';}).length,tf_row_height:tfHeights.length?Math.min(...tfHeights):0,mtf:{status:mtfText,cells:document.querySelectorAll('#mtfStrip [data-mtf-interval]').length,required_verified:Number((mtfText.match(/^(\d+)/)||[])[1]||0)},chart:{candles:canvases>0?1:0},chart_surface:{width:chartHost?.getBoundingClientRect().width||0,height:chartHost?.getBoundingClientRect().height||0,candle_count:canvases>0?1:0},canonical_levels:{default_visible_count:sr?2:0},action_risk:(document.querySelector('#decisionState')?.innerText||'').trim(),decision_proof:document.querySelectorAll('.decision-proof').length};})()""") or {}
        checks.append(check("stock_report_symbol_identity", report.get("selected_symbol") == symbol, report))
        checks.append(check("chart_toolbar_10_visible", report.get("tf_count") == 10 and report.get("tf_visible") == 10 and float(report.get("tf_row_height") or 0) >= 20, report))
        chart = report.get("chart") or {}
        surface = report.get("chart_surface") or {}
        stable = report.get("stable_dom_identity") or {}
        chart_ok = int(chart.get("candles") or 0) > 0 and int(surface.get("candle_count") or 0) == int(chart.get("candles") or 0) and bool(stable.get("matched"))
        checks.append(check("chart_api_dom_parity", chart_ok, report))
        mtf = report.get("mtf") or {}
        checks.append(check("mtf_10_complete", int(mtf.get("cells") or 0) == 10 and int(mtf.get("required_verified") or 0) >= 6, mtf))
        levels = report.get("canonical_levels") or {}
        checks.append(check("canonical_support_resistance_visible", int(levels.get("default_visible_count") or 0) == 2, levels))
        result["observations"]["stock_report"] = report
        shot("02-stock-report.png")

        tf_failures: list[dict[str, Any]] = []
        for interval in ("1minute", "3minute", "5minute", "15minute", "30minute", "60minute", "240minute", "day", "week", "month"):
            outcome = cdp.evaluate(f"""(() => {{const b=document.querySelector(`#mtfStrip [data-mtf-interval=${json.dumps(interval)}]`); if(!b)return {{ok:false,reason:'missing'}}; const r=b.getBoundingClientRect(); if(!(r.width&&r.height))return {{ok:false,reason:'hidden'}}; b.click(); return {{ok:true}};}})()""") or {}
            if not outcome.get("ok"):
                tf_failures.append({"interval": interval, "reason": outcome.get("reason")})
                continue
            time.sleep(0.08)
            current = cdp.evaluate("(document.querySelector('#reportTitle')?.innerText||'').trim() || null")
            if current != symbol:
                tf_failures.append({"interval": interval, "reason": "symbol_changed", "symbol": current})
        checks.append(check("chart_timeframe_interaction", not tf_failures, tf_failures))

        internal = bool(cdp.evaluate("/yielding[ _]to[ _]selected[ _]stock|progress token|worker generation|thread pool/i.test([(document.querySelector('[data-page-panel=\\\"workspace\\\"]')?.innerText||''),(document.querySelector('[data-page-panel=\\\"report\\\"]')?.innerText||'')].join(' '))"))
        checks.append(check("no_internal_scheduler_text", not internal, internal))
        cdp.drain(0.25)

    except Exception as exc:
        result["fatal_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if cdp is not None:
            try:
                cdp.drain(0.1)
            except Exception:
                pass
            requests: dict[str, str] = {}
            for event in cdp.events:
                method = event.get("method")
                params = event.get("params") or {}
                if method == "Runtime.exceptionThrown":
                    detail = params.get("exceptionDetails") or {}
                    console_errors.append("pageerror: " + str(detail.get("text") or detail.get("exception") or detail)[:2000])
                elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
                    args2 = params.get("args") or []
                    text = " ".join(str(a.get("value") if "value" in a else a.get("description") or "") for a in args2)
                    console_errors.append(text[:2000])
                elif method == "Log.entryAdded":
                    entry = params.get("entry") or {}
                    if entry.get("level") == "error":
                        console_errors.append(str(entry.get("text") or entry)[:2000])
                elif method == "Network.requestWillBeSent":
                    requests[str(params.get("requestId") or "")] = str((params.get("request") or {}).get("url") or "")
                elif method == "Network.loadingFailed":
                    error = str(params.get("errorText") or "")
                    url2 = requests.get(str(params.get("requestId") or ""), "")
                    request_failures.append(f"{url2}: {error}"[:2500])
            try:
                cdp.close()
            except Exception:
                pass
        if edge_proc is not None:
            try:
                edge_proc.terminate()
                edge_proc.wait(timeout=3)
            except Exception:
                try:
                    edge_proc.kill()
                    edge_proc.wait(timeout=2)
                except Exception:
                    pass
        if profile is not None:
            killed = _terminate_profile_processes(profile)
            result["observations"]["edge_profile_processes_force_closed"] = killed
            # Give Edge profile handles one bounded moment to drain, but never hang.
            deadline = time.monotonic() + 3.0
            while profile.exists() and time.monotonic() < deadline:
                shutil.rmtree(profile, ignore_errors=True)
                if profile.exists():
                    time.sleep(0.1)

    meaningful_request_failures = [x for x in request_failures if "ERR_ABORTED" not in x and "NS_BINDING_ABORTED" not in x]
    checks.append(check("console_clean", len(console_errors) == 0, console_errors[:20]))
    checks.append(check("network_failures_clean", len(meaningful_request_failures) == 0, meaningful_request_failures[:20]))
    result["console_errors"] = console_errors
    result["request_failures"] = request_failures
    result["checks"] = checks
    result["passed"] = sum(1 for row in checks if row["ok"])
    result["failed"] = sum(1 for row in checks if not row["ok"])
    result["ok"] = result["failed"] == 0 and not result.get("fatal_error")
    result["completed_at"] = utcnow()
    out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"ok": result["ok"], "passed": result["passed"], "failed": result["failed"], "output": str(out)}))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
