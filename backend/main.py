from __future__ import annotations

import signal
import sys

# Publish the live module identity before importing routes. This prevents a
# second runtime from being constructed when compatibility code imports main.
if __name__ == "__main__":
    sys.modules.setdefault("main", sys.modules[__name__])

from application_runtime import *  # noqa: F401,F403 - compatibility facade
import application_runtime as runtime
from core.runtime_control import CONTROL
from core.runtime_logging import log_line
from core.runtime_primitives import *  # noqa: F401,F403 - compatibility facade
from http_server import Handler, serve


def stop(*_args) -> None:
    CONTROL.stop()
    runtime.APP.event("INFO", "runtime", "Shutdown requested", {})
    try:
        runtime.APP.supervisor.stop()
    except Exception:
        pass
    try:
        runtime.APP.production_data_plane.close()
    except Exception:
        pass
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    CONTROL.reset()
    runtime.APP.start()
    serve(runtime.APP)


if __name__ == "__main__":
    main()
