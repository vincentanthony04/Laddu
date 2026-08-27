#!/usr/bin/env python3
"""Non-destructive QuestDB container promotion for Project Laddu.

Stdlib-only helper so the exact Docker rename transaction can be regression-tested
without relying on Windows PowerShell native-command exit-code behaviour.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

AUTHORITATIVE = "project-laddu-questdb"
CANDIDATE_PREFIX = "project-laddu-questdb-candidate-"
RETAINED_PREFIX = "project-laddu-questdb-retained-"
IMAGE = "questdb/questdb:9.4.3"


class RecoveryError(RuntimeError):
    pass


@dataclass
class DockerResult:
    code: int
    stdout: str
    stderr: str


class Docker:
    def __init__(self, command: str) -> None:
        self.command = command

    def run(self, args: Iterable[str], *, check: bool = False) -> DockerResult:
        proc = subprocess.run(
            [self.command, *list(args)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        result = DockerResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())
        if check and result.code != 0:
            detail = result.stderr or result.stdout or f"exit {result.code}"
            raise RecoveryError(f"docker {' '.join(args)} failed: {detail}")
        return result

    def inspect(self, ref: str) -> dict[str, Any] | None:
        result = self.run(["inspect", ref])
        if result.code != 0 or not result.stdout:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RecoveryError(f"docker inspect returned invalid JSON for {ref}: {exc}") from exc
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            return None
        return payload[0]

    def names(self, prefix: str) -> list[str]:
        result = self.run(["ps", "-a", "--filter", f"name={prefix}", "--format", "{{.Names}}"], check=True)
        return sorted({line.strip() for line in result.stdout.splitlines() if line.strip().startswith(prefix)})

    def names_by_volume(self, volume: str) -> list[str]:
        result = self.run(["ps", "-a", "--filter", f"volume={volume}", "--format", "{{.Names}}"], check=True)
        return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})

    def rename(self, ref: str, new_name: str) -> None:
        self.run(["rename", ref, new_name], check=True)

    def logs(self, ref: str, tail: int = 200) -> str:
        result = self.run(["logs", "--tail", str(max(1, tail)), ref])
        return (result.stdout + ("\n" if result.stdout and result.stderr else "") + result.stderr).strip()

    def stop(self, ref: str, timeout: int = 30) -> None:
        result = self.run(["stop", "-t", str(max(1, timeout)), ref])
        if result.code != 0:
            detail = result.stderr or result.stdout or f"exit {result.code}"
            raise RecoveryError(f"docker stop {ref} failed: {detail}")

    def create_candidate(self, name: str, volume: str, port: int) -> str:
        result = self.run(
            [
                "run", "-d", "--name", name, "--restart", "unless-stopped",
                "--memory", "3g", "-p", f"127.0.0.1:{port}:9000",
                "-v", f"{volume}:/var/lib/questdb",
                "-e", "QDB_HTTP_BIND_TO=0.0.0.0:9000",
                "-e", "QDB_LINE_HTTP_ENABLED=true",
                "-e", "QDB_PG_ENABLED=true",
                IMAGE,
            ],
            check=True,
        )
        return result.stdout.strip()


def container_id(item: dict[str, Any] | None) -> str:
    return str((item or {}).get("Id") or "").strip()


def container_status(item: dict[str, Any] | None) -> str:
    return str(((item or {}).get("State") or {}).get("Status") or "missing").strip().lower()


def mounted_volume(item: dict[str, Any] | None) -> str:
    for mount in list((item or {}).get("Mounts") or []):
        if str(mount.get("Destination") or "") == "/var/lib/questdb":
            return str(mount.get("Name") or mount.get("Source") or "").strip()
    return ""


def published_http_port(item: dict[str, Any] | None) -> int:
    ports = ((item or {}).get("NetworkSettings") or {}).get("Ports") or {}
    bindings = ports.get("9000/tcp") or []
    if not bindings:
        bindings = (((item or {}).get("HostConfig") or {}).get("PortBindings") or {}).get("9000/tcp") or []
    for binding in bindings:
        try:
            port = int(str(binding.get("HostPort") or "0"))
        except (TypeError, ValueError):
            continue
        if 0 < port <= 65535:
            return port
    return 0


def endpoint_healthy(port: int, timeout: float = 3.0) -> bool:
    if port <= 0:
        return False
    query = urllib.parse.quote("select 1")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/exec?query={query}", timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except Exception:
        return False


def compact_logs(text: str, limit: int = 1800) -> str:
    cleaned = " | ".join(line.strip() for line in str(text or "").splitlines() if line.strip())
    return cleaned[-limit:] if len(cleaned) > limit else cleaned


def wait_candidate(docker: Docker, name: str, port: int, seconds: int) -> tuple[bool, str]:
    """Wait for QuestDB while failing fast on terminal/lock-conflict states."""
    deadline = time.monotonic() + max(30, seconds)
    last_status = "missing"
    while time.monotonic() < deadline:
        item = docker.inspect(name)
        last_status = container_status(item)
        if last_status in {"exited", "dead", "removing"}:
            return False, f"container_status={last_status}; logs={compact_logs(docker.logs(name))}"
        if last_status == "running" and endpoint_healthy(port):
            return True, f"container_status={last_status}; endpoint=healthy"
        if last_status == "restarting":
            logs = compact_logs(docker.logs(name))
            lowered = logs.lower()
            if "cannot lock table name registry file" in lowered or ("cannot lock" in lowered and "/var/lib/questdb/db" in lowered):
                return False, f"container_status={last_status}; fatal_volume_lock_conflict=true; logs={logs}"
        time.sleep(3)
    return False, f"container_status={last_status}; readiness_timeout={max(30, seconds)}s; logs={compact_logs(docker.logs(name))}"


def excluded_ranges() -> list[tuple[int, int]]:
    if platform.system().lower() != "windows":
        return []
    try:
        proc = subprocess.run(
            ["netsh.exe", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return []
    ranges: list[tuple[int, int]] = []
    for line in proc.stdout.splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)(?:\s+\*)?\s*$", line)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if 0 < start <= end <= 65535:
                ranges.append((start, end))
    return ranges


def port_excluded(port: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= port <= end for start, end in ranges)


def port_bindable(port: int) -> bool:
    if port < 1024 or port > 65535:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def find_safe_port(preferred: int, rejected: set[int] | None = None) -> tuple[int, str, list[tuple[int, int]]]:
    rejected = rejected or set()
    ranges = excluded_ranges()
    candidates: list[int] = []
    if 1024 <= preferred <= 65535:
        candidates.append(preferred)
    for start, end in ((59100, 59999), (61000, 61999), (45000, 48999), (30000, 31999)):
        candidates.extend(range(start, end + 1))
    seen: set[int] = set()
    for port in candidates:
        if port in seen or port in rejected:
            continue
        seen.add(port)
        if port_excluded(port, ranges):
            continue
        if port_bindable(port):
            return port, ("PREFERRED_PORT_BIND_PROVEN" if port == preferred else "SAFE_PORT_DISCOVERED"), ranges
    raise RecoveryError("No safe loopback port is available for QuestDB HTTP.")


def same_volume(item: dict[str, Any] | None, expected: str) -> bool:
    actual = mounted_volume(item)
    return bool(actual and expected and actual == expected)


def installer_owned_name(name: str) -> bool:
    name = str(name or "").strip()
    return name == AUTHORITATIVE or name.startswith(CANDIDATE_PREFIX) or name.startswith(RETAINED_PREFIX)


def live_status(status: str) -> bool:
    return str(status or "").lower() in {"running", "restarting", "created", "paused"}


def reconcile_volume_owners(
    docker: Docker, volume: str
) -> tuple[tuple[str, dict[str, Any], int] | None, tuple[str, dict[str, Any], int] | None, list[dict[str, Any]]]:
    """Enforce one live QuestDB owner for the retained volume before recovery.

    The real Windows failure behind PL46 R5 was a restarting QuestDB container
    still owning /var/lib/questdb while a recovery candidate was attached to the
    same volume. QuestDB correctly refused the second writer with
    `cannot lock table name registry file`. This transaction inventories *all*
    containers mounting the volume, not merely candidate-prefix containers.

    Unknown containers are never stopped: their presence fails closed. Only the
    authoritative Project Laddu container and installer-owned candidate/retained
    containers may be quiesced. At most one already-healthy owner is preserved.
    """
    names = list(docker.names_by_volume(volume))
    old = docker.inspect(AUTHORITATIVE)
    if old is not None and same_volume(old, volume) and AUTHORITATIVE not in names:
        names.append(AUTHORITATIVE)
    names = sorted(set(names))

    unknown = [name for name in names if not installer_owned_name(name)]
    if unknown:
        raise RecoveryError(
            "QuestDB retained volume has non-Project-Laddu container owner(s); "
            "installer will not stop or mutate them: " + ", ".join(unknown)
        )

    snapshots: dict[str, tuple[dict[str, Any], str, int, bool]] = {}
    healthy_authoritative: tuple[str, dict[str, Any], int] | None = None
    healthy_candidates: list[tuple[str, dict[str, Any], int]] = []
    for name in names:
        item = docker.inspect(name)
        if item is None or not same_volume(item, volume):
            continue
        status = container_status(item)
        port = published_http_port(item)
        healthy = status == "running" and endpoint_healthy(port)
        snapshots[name] = (item, status, port, healthy)
        if name == AUTHORITATIVE and healthy:
            healthy_authoritative = (name, item, port)
        elif name.startswith(CANDIDATE_PREFIX) and healthy:
            healthy_candidates.append((name, item, port))

    healthy_count = (1 if healthy_authoritative else 0) + len(healthy_candidates)
    if healthy_count > 1:
        owners = ([healthy_authoritative[0]] if healthy_authoritative else []) + [row[0] for row in healthy_candidates]
        raise RecoveryError("Multiple healthy QuestDB owners exist for the retained volume: " + ", ".join(owners))

    chosen_name = healthy_authoritative[0] if healthy_authoritative else (healthy_candidates[0][0] if healthy_candidates else "")
    evidence: list[dict[str, Any]] = []
    for name, (item, status, port, healthy) in snapshots.items():
        if name == chosen_name:
            continue
        row = {
            "name": name,
            "container_id": container_id(item),
            "status_before": status,
            "port": port,
            "healthy_before": healthy,
            "logs": compact_logs(docker.logs(name)),
        }
        if live_status(status):
            docker.stop(name, timeout=30)
            after = container_status(docker.inspect(name))
            row["status_after"] = after
            if live_status(after):
                raise RecoveryError(f"QuestDB volume owner did not quiesce after docker stop: {name} status={after}")
        else:
            row["status_after"] = status
        evidence.append(row)

    # Re-query the volume after quiescence. There must be no live owner other than
    # the single explicitly chosen healthy authority/candidate.
    residual_live: list[str] = []
    for name in docker.names_by_volume(volume):
        item = docker.inspect(name)
        if item is None or not same_volume(item, volume):
            continue
        status = container_status(item)
        if live_status(status) and name != chosen_name:
            residual_live.append(f"{name}:{status}")
    if residual_live:
        raise RecoveryError("QuestDB retained volume still has conflicting live owner(s): " + ", ".join(residual_live))

    chosen_candidate = healthy_candidates[0] if healthy_candidates else None
    return healthy_authoritative, chosen_candidate, evidence


def reconcile_candidates(docker: Docker, volume: str) -> tuple[tuple[str, dict[str, Any], int] | None, list[dict[str, Any]]]:
    """Reuse one healthy candidate and quiesce only installer-owned unhealthy candidates.

    A failed prior installer may have left a running candidate attached to the retained
    QuestDB volume. Starting another candidate against that same volume is unsafe. We
    therefore capture diagnostics and stop only our candidate-prefix containers before
    a new attempt. The named volume and historical authoritative container are untouched.
    """
    healthy: list[tuple[str, dict[str, Any], int]] = []
    quiesced: list[dict[str, Any]] = []
    for name in docker.names(CANDIDATE_PREFIX):
        item = docker.inspect(name)
        if not same_volume(item, volume):
            continue
        status = container_status(item)
        port = published_http_port(item)
        if status == "running" and endpoint_healthy(port):
            healthy.append((name, item or {}, port))
            continue
        evidence = {
            "name": name,
            "container_id": container_id(item),
            "status_before": status,
            "port": port,
            "logs": compact_logs(docker.logs(name)),
        }
        if status in {"running", "restarting", "created", "paused"}:
            docker.stop(name, timeout=30)
            evidence["status_after"] = container_status(docker.inspect(name))
        else:
            evidence["status_after"] = status
        quiesced.append(evidence)
    if len(healthy) > 1:
        names = ", ".join(name for name, _, _ in healthy)
        raise RecoveryError(f"Multiple healthy QuestDB candidates exist for the retained volume: {names}")
    return (healthy[0] if healthy else None), quiesced


def unique_retained_name(docker: Docker) -> str:
    for _ in range(20):
        name = f"project-laddu-questdb-retained-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        if docker.inspect(name) is None:
            return name
    raise RecoveryError("Unable to allocate a unique retained QuestDB container name.")


def promote(docker: Docker, candidate_name: str) -> tuple[str, str]:
    candidate = docker.inspect(candidate_name)
    candidate_id = container_id(candidate)
    if not candidate_id:
        raise RecoveryError(f"Healthy QuestDB candidate disappeared before promotion: {candidate_name}")

    old = docker.inspect(AUTHORITATIVE)
    old_id = container_id(old)
    retained = ""
    if old_id:
        retained = unique_retained_name(docker)
        docker.rename(old_id, retained)
        if docker.inspect(AUTHORITATIVE) is not None:
            raise RecoveryError("Authoritative QuestDB name is still occupied after retaining the historical container.")
        retained_item = docker.inspect(retained)
        if container_id(retained_item) != old_id:
            raise RecoveryError("Historical QuestDB container rename could not be verified by container ID.")

    try:
        docker.rename(candidate_id, AUTHORITATIVE)
        promoted = docker.inspect(AUTHORITATIVE)
        if container_id(promoted) != candidate_id:
            raise RecoveryError("Candidate promotion could not be verified by container ID.")
    except Exception as exc:
        if old_id and retained:
            try:
                if docker.inspect(AUTHORITATIVE) is None:
                    docker.rename(old_id, AUTHORITATIVE)
            except Exception as rollback_exc:
                raise RecoveryError(f"Candidate promotion failed and historical-name rollback also failed: {rollback_exc}") from exc
        raise
    return retained, candidate_id


def recover(docker: Docker, volume: str, preferred_port: int, timeout: int) -> dict[str, Any]:
    old = docker.inspect(AUTHORITATIVE)
    if old is not None and not same_volume(old, volume):
        raise RecoveryError("Authoritative QuestDB container does not mount the expected retained volume.")

    healthy_authoritative, found, quiesced_owners = reconcile_volume_owners(docker, volume)
    if healthy_authoritative:
        _, current, old_port = healthy_authoritative
        return {
            "ok": True,
            "port": old_port,
            "selection": "EXISTING_BINDING",
            "action": "PRESERVED_RUNNING_CONTAINER",
            "retained_container": "",
            "volume_name": mounted_volume(current) or volume,
            "candidate_reused": False,
            "container_id": container_id(current),
            "quiesced_volume_owners": quiesced_owners,
        }

    candidate_reused = found is not None
    selection = "EXISTING_HEALTHY_CANDIDATE"
    if found:
        candidate_name, candidate, port = found
    else:
        rejected: set[int] = set()
        candidate_name = ""
        candidate = {}
        for _ in range(5):
            port, selection, _ = find_safe_port(preferred_port, rejected)
            candidate_name = CANDIDATE_PREFIX + uuid.uuid4().hex[:10]
            try:
                docker.create_candidate(candidate_name, volume, port)
            except RecoveryError:
                rejected.add(port)
                continue
            ready, readiness_detail = wait_candidate(docker, candidate_name, port, timeout)
            if not ready:
                # Preserve diagnostic identity but release the volume from a live failed process
                # so a subsequent installer run cannot attach a second running QuestDB to it.
                current = docker.inspect(candidate_name)
                if container_status(current) in {"running", "restarting", "created", "paused"}:
                    docker.stop(candidate_name, timeout=30)
                raise RecoveryError(
                    f"QuestDB candidate did not become healthy on port {port}; "
                    f"candidate retained stopped as {candidate_name}; {readiness_detail}"
                )
            candidate = docker.inspect(candidate_name) or {}
            break
        else:
            raise RecoveryError("Docker rejected five independently bind-proven QuestDB ports.")

    if container_status(candidate) != "running" or not endpoint_healthy(port):
        raise RecoveryError(f"Selected QuestDB candidate is not healthy: {candidate_name}")
    if not same_volume(candidate, volume):
        raise RecoveryError(f"Selected QuestDB candidate does not mount retained volume {volume}: {candidate_name}")

    retained, promoted_id = promote(docker, candidate_name)
    promoted = docker.inspect(AUTHORITATIVE)
    promoted_port = published_http_port(promoted)
    if promoted_port != port or not endpoint_healthy(promoted_port):
        raise RecoveryError("Promoted QuestDB container is not healthy on the proven candidate port.")
    return {
        "ok": True,
        "port": promoted_port,
        "selection": selection,
        "action": "HEALTHY_CANDIDATE_REUSED_AND_PROMOTED" if candidate_reused else "BLUE_GREEN_CANDIDATE_PROMOTED",
        "retained_container": retained,
        "volume_name": volume,
        "candidate_reused": candidate_reused,
        "container_id": promoted_id,
        "quiesced_volume_owners": quiesced_owners,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--volume-name", required=True)
    parser.add_argument("--preferred-port", type=int, default=59000)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--docker-command", default=os.environ.get("LADDU_DOCKER_COMMAND") or ("docker.exe" if os.name == "nt" else "docker"))
    args = parser.parse_args()
    try:
        result = recover(Docker(args.docker_command), args.volume_name, args.preferred_port, args.timeout)
    except Exception as exc:
        print(f"[QUESTDB RECOVERY FAILED] {exc}", file=sys.stderr)
        return 1
    print("LADDU_RECOVERY_RESULT_JSON=" + json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
