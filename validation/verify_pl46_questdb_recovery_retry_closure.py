#!/usr/bin/env python3
"""Focused regression for QuestDB retained-volume single-owner recovery.

Covers the exact Windows R5 failure where a restarting owner retained the
/var/lib/questdb lock while the installer attached a second recovery candidate.
"""
from __future__ import annotations
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "installer" / "questdb_recovery.py"
spec = importlib.util.spec_from_file_location("laddu_questdb_recovery", SRC)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

VOL = "project-laddu-data-plane_laddu_questdb"
OTHER = "other-volume"

def item(name: str, status: str, port: int = 0, volume: str = VOL):
    ports = {"9000/tcp": ([{"HostPort": str(port)}] if port else [])}
    return {
        "Id": f"id-{name}",
        "Name": name,
        "State": {"Status": status},
        "Mounts": [{"Destination": "/var/lib/questdb", "Name": volume}],
        "NetworkSettings": {"Ports": ports},
        "HostConfig": {"PortBindings": ports},
    }

class FakeDocker:
    def __init__(self, items=None, logs=None):
        self.items = dict(items or {})
        self.log_map = dict(logs or {})
        self.stopped = []
    def names(self, prefix):
        return sorted(n for n in self.items if n.startswith(prefix))
    def names_by_volume(self, volume):
        out=[]
        for n,v in self.items.items():
            if mod.mounted_volume(v) == volume:
                out.append(n)
        return sorted(out)
    def inspect(self, ref):
        # Support container IDs used by promote() in case a future extension calls it.
        if ref in self.items:
            return self.items[ref]
        for v in self.items.values():
            if v.get("Id") == ref:
                return v
        return None
    def logs(self, ref, tail=200):
        return self.log_map.get(ref, f"diagnostic-log-for-{ref}")
    def stop(self, ref, timeout=30):
        self.stopped.append(ref)
        self.items[ref]["State"]["Status"] = "exited"

orig_health = mod.endpoint_healthy
try:
    healthy_ports = set()
    mod.endpoint_healthy = lambda port, timeout=3.0: port in healthy_ports

    # 1) Exact real failure: authoritative container is RESTARTING and still owns
    # the volume. It must be quiesced before any candidate can be created.
    docker = FakeDocker({mod.AUTHORITATIVE: item(mod.AUTHORITATIVE, "restarting", 59000)})
    auth, cand, evidence = mod.reconcile_volume_owners(docker, VOL)
    assert auth is None and cand is None
    assert docker.stopped == [mod.AUTHORITATIVE], docker.stopped
    assert evidence[0]["status_before"] == "restarting" and evidence[0]["status_after"] == "exited"

    # 2) Restarting authoritative + already healthy candidate: stop only the bad
    # authority and reuse the proven candidate; never attach a third writer.
    candidate = mod.CANDIDATE_PREFIX + "healthy"
    healthy_ports = {59178}
    docker = FakeDocker({
        mod.AUTHORITATIVE: item(mod.AUTHORITATIVE, "restarting", 59000),
        candidate: item(candidate, "running", 59178),
    })
    auth, cand, evidence = mod.reconcile_volume_owners(docker, VOL)
    assert auth is None and cand and cand[0] == candidate
    assert docker.stopped == [mod.AUTHORITATIVE]

    # 3) Healthy authoritative wins. Unhealthy/restarting candidate and retained
    # installer-owned containers are quiesced; authority remains untouched.
    bad_candidate = mod.CANDIDATE_PREFIX + "bad"
    retained = mod.RETAINED_PREFIX + "old"
    healthy_ports = {59000}
    docker = FakeDocker({
        mod.AUTHORITATIVE: item(mod.AUTHORITATIVE, "running", 59000),
        bad_candidate: item(bad_candidate, "restarting", 59178),
        retained: item(retained, "paused", 0),
    })
    auth, cand, evidence = mod.reconcile_volume_owners(docker, VOL)
    assert auth and auth[0] == mod.AUTHORITATIVE and cand is None
    assert set(docker.stopped) == {bad_candidate, retained}
    assert mod.AUTHORITATIVE not in docker.stopped

    # 4) Exited authoritative owns metadata but no live lock: do not issue a
    # pointless stop, and permit fresh recovery candidate creation later.
    healthy_ports = set()
    docker = FakeDocker({mod.AUTHORITATIVE: item(mod.AUTHORITATIVE, "exited", 59000)})
    auth, cand, evidence = mod.reconcile_volume_owners(docker, VOL)
    assert auth is None and cand is None and docker.stopped == []
    assert evidence and evidence[0]["status_after"] == "exited"

    # 5) Paused authoritative is a live volume owner and must be explicitly
    # quiesced before recovery.
    docker = FakeDocker({mod.AUTHORITATIVE: item(mod.AUTHORITATIVE, "paused", 59000)})
    mod.reconcile_volume_owners(docker, VOL)
    assert docker.stopped == [mod.AUTHORITATIVE]

    # 6) Unknown owner: fail closed and never stop a container the installer does
    # not own, even though it mounts the same retained volume.
    alien = "manual-questdb-debug"
    docker = FakeDocker({alien: item(alien, "running", 59999)})
    try:
        mod.reconcile_volume_owners(docker, VOL)
        raise AssertionError("unknown volume owner was not rejected")
    except mod.RecoveryError as exc:
        assert "non-Project-Laddu" in str(exc) and alien in str(exc)
    assert docker.stopped == []

    # 7) Containers on another volume are irrelevant and untouched.
    other_candidate = mod.CANDIDATE_PREFIX + "other-volume"
    docker = FakeDocker({other_candidate: item(other_candidate, "running", 59180, OTHER)})
    auth, cand, evidence = mod.reconcile_volume_owners(docker, VOL)
    assert auth is None and cand is None and evidence == [] and docker.stopped == []

    # 8) The exact QuestDB lock signature must fail readiness immediately instead
    # of consuming the 600-second recovery window.
    lock_name = mod.CANDIDATE_PREFIX + "lock"
    docker = FakeDocker(
        {lock_name: item(lock_name, "restarting", 59178)},
        {lock_name: "io.questdb.cairo.CairoException: [0] cannot lock table name registry file [path=/var/lib/questdb/db]"},
    )
    ready, detail = mod.wait_candidate(docker, lock_name, 59178, 600)
    assert ready is False
    assert "fatal_volume_lock_conflict=true" in detail
    assert "cannot lock table name registry file" in detail

    # 9) Existing prior retry closure remains: an unhealthy installer-owned
    # candidate is stopped; an other-volume candidate is untouched.
    docker = FakeDocker({
        mod.CANDIDATE_PREFIX + "bad2": item(mod.CANDIDATE_PREFIX + "bad2", "running", 59179),
        mod.CANDIDATE_PREFIX + "stopped": item(mod.CANDIDATE_PREFIX + "stopped", "exited", 59181),
        mod.CANDIDATE_PREFIX + "other": item(mod.CANDIDATE_PREFIX + "other", "running", 59182, OTHER),
    })
    found, old_evidence = mod.reconcile_candidates(docker, VOL)
    assert found is None
    assert docker.stopped == [mod.CANDIDATE_PREFIX + "bad2"]
    assert len(old_evidence) == 2

    source = SRC.read_text(encoding="utf-8")
    data_plane = (ROOT / "installer" / "data_plane.ps1").read_text(encoding="utf-8")
    assert 'names_by_volume' in source
    assert 'non-Project-Laddu container owner' in source
    assert 'fatal_volume_lock_conflict=true' in source
    assert 'quiesced_volume_owners' in source
    assert 'default=600' in source
    assert '-ReadyTimeoutSeconds 600' in data_plane
    assert 'candidate retained stopped as' in source
finally:
    mod.endpoint_healthy = orig_health

print(json.dumps({
    "ok": True,
    "proof": "QuestDB retained volume has exactly one live Project-Laddu owner before recovery; real restarting-owner lock conflict fails fast",
    "states_proven": ["running_healthy", "restarting", "paused", "exited", "candidate_reuse", "retained_owner", "unknown_owner_fail_closed", "fatal_lock_fast_fail"],
    "ready_timeout_seconds": 600,
    "broker_authority": "NONE",
}, indent=2))
