from __future__ import annotations

from dataclasses import dataclass
import os


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DataPlaneSettings:
    mode: str
    operational_dsn: str
    governance_dsn: str
    questdb_http_url: str
    questdb_username: str | None
    questdb_password: str | None
    questdb_flush_ms: int
    questdb_batch_size: int
    questdb_max_queue_age_ms: int
    require_operational: bool
    require_governance: bool
    require_questdb: bool

    @classmethod
    def from_env(cls) -> "DataPlaneSettings":
        mode = os.environ.get("PROJECT_LADDU_DATA_PLANE_MODE", "test").strip().lower()
        if mode not in {"production", "test"}:
            raise ValueError(f"Unsupported PROJECT_LADDU_DATA_PLANE_MODE={mode!r}")
        production = mode == "production"
        return cls(
            mode=mode,
            operational_dsn=os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", "").strip(),
            governance_dsn=os.environ.get("PROJECT_LADDU_GOVERNANCE_DSN", "").strip(),
            questdb_http_url=os.environ.get("PROJECT_LADDU_QUESTDB_HTTP_URL", "http://127.0.0.1:59000").rstrip("/"),
            questdb_username=os.environ.get("PROJECT_LADDU_QUESTDB_USERNAME") or None,
            questdb_password=os.environ.get("PROJECT_LADDU_QUESTDB_PASSWORD") or None,
            questdb_flush_ms=max(50, int(os.environ.get("PROJECT_LADDU_QUESTDB_FLUSH_MS", "250"))),
            questdb_batch_size=max(10, min(5000, int(os.environ.get("PROJECT_LADDU_QUESTDB_BATCH_SIZE", "1000")))),
            questdb_max_queue_age_ms=max(250, int(os.environ.get("PROJECT_LADDU_QUESTDB_MAX_QUEUE_AGE_MS", "2000"))),
            require_operational=_bool("PROJECT_LADDU_REQUIRE_OPERATIONAL_POSTGRES", production),
            require_governance=_bool("PROJECT_LADDU_REQUIRE_GOVERNANCE_POSTGRES", production),
            require_questdb=_bool("PROJECT_LADDU_REQUIRE_QUESTDB", production),
        )
