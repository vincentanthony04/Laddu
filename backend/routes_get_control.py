from __future__ import annotations

from routes_get_dependencies import *

def r_runtime_controller(app, qs, q, mode):
    """Event-driven Level-5 blocker and safe-recovery control plane."""
    controller = getattr(app, "autonomic_controller", None)
    if controller is None:
        return ({"ok": False, "state": "CONTROLLER_UNAVAILABLE"}, 503)
    return controller.snapshot(refresh=_flag(qs, "refresh"))


def r_runtime_controller_events(app, qs, q, mode):
    controller = getattr(app, "autonomic_controller", None)
    if controller is None:
        return ({"ok": False, "state": "CONTROLLER_UNAVAILABLE"}, 503)
    try:
        after_sequence = max(0, int(qs.get("after_sequence", [0])[0] or 0))
    except (TypeError, ValueError):
        after_sequence = 0
    try:
        limit = max(1, min(500, int(qs.get("limit", [200])[0] or 200)))
    except (TypeError, ValueError):
        limit = 200
    return controller.events(after_sequence=after_sequence, limit=limit)


def r_runtime_controller_component(app, qs, q, mode):
    component = str(qs.get("component", [q])[0] or q or "").strip()
    if not component:
        return ({"ok": False, "error": "component_required"}, 400)
    snapshot = getattr(getattr(app, "supervisor", None), "snapshot", lambda: {})()
    row = dict(snapshot.get(component) or {})
    if not row:
        return ({"ok": False, "state": "UNKNOWN_COMPONENT", "component": component}, 404)
    return {"ok": True, "component": component, **row}


def r_level5_control_plane(app, qs, q, mode):
    return r_runtime_controller(app, qs, q, mode)


def _operations_service(app):
    service = getattr(app, "operations_control", None)
    if service is None:
        return None, ({"ok": False, "state": "OPERATIONS_CONTROL_UNAVAILABLE"}, 503)
    return service, None


def r_operations_summary(app, qs, q, mode):
    service, error = _operations_service(app)
    if error:
        return error
    if _flag(qs, "refresh") or _flag(qs, "live"):
        return service.live_summary()
    return service.summary()


def r_operations_jobs(app, qs, q, mode):
    service, error = _operations_service(app)
    return error or service.jobs()


def r_operations_events(app, qs, q, mode):
    service, error = _operations_service(app)
    if error:
        return error
    try:
        after_sequence = max(0, int(qs.get("after_sequence", [0])[0] or 0))
        limit = max(1, min(1000, int(qs.get("limit", [250])[0] or 250)))
    except (TypeError, ValueError):
        return ({"ok": False, "error": "invalid operations event range"}, 400)
    return service.events(after_sequence=after_sequence, limit=limit)


def r_operations_logs(app, qs, q, mode):
    service, error = _operations_service(app)
    if error:
        return error
    if _flag(qs, "refresh"):
        try:
            service.refresh_logs()
        except Exception:
            pass
    component = str(qs.get("component", [""])[0] or "")
    level = str(qs.get("level", [""])[0] or "")
    try:
        limit = max(20, min(1000, int(qs.get("limit", [250])[0] or 250)))
    except (TypeError, ValueError):
        return ({"ok": False, "error": "invalid log limit"}, 400)
    return service.logs(component=component, level=level, limit=limit)


def r_operations_maturity_blockers(app, qs, q, mode):
    service, error = _operations_service(app)
    if error:
        return error
    summary = service.summary()
    controller = dict(summary.get("controller") or {})
    return {
        "ok": True,
        "time": summary.get("time"),
        "primary_blocker": controller.get("primary_blocker"),
        "blockers": list(controller.get("blockers") or []),
        "counts": summary.get("counts") or {},
    }


def r_workload_governor(app, qs, q, mode):
    governor = getattr(app, "workload_governor", None)
    if governor is None:
        return ({"ok": False, "state": "WORKLOAD_GOVERNOR_UNAVAILABLE"}, 503)
    return {"ok": True, "time": now_iso(), "governor": governor.snapshot()}
