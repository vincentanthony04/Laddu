from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib import parse, request


def _load_psycopg():
    try:
        import psycopg
        from psycopg import sql
    except Exception as exc:
        raise RuntimeError('psycopg[binary,pool] is required before data-plane provisioning') from exc
    return psycopg, sql


def _split_sql(text: str) -> list[str]:
    """Split QuestDB DDL; the shipped file contains no procedural bodies."""
    return [part.strip() for part in text.split(';') if part.strip()]



def _app_dsn(admin_dsn: str, role: str, password: str) -> str:
    parsed = parse.urlsplit(admin_dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("PostgreSQL DSN must use postgres:// or postgresql://")
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    userinfo = f"{parse.quote(role, safe='')}:{parse.quote(password, safe='')}@"
    return parse.urlunsplit((parsed.scheme, userinfo + host + port, parsed.path, parsed.query, parsed.fragment))


def smoke_operational_repository(admin_dsn: str, app_role: str, app_password: str) -> dict:
    """Run the exact operational repository SQL through a real Psycopg cursor."""
    backend = Path(__file__).resolve().parents[1]
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from core.data_plane.instrument_repository import ProductionInstrumentRepository
    from core.data_plane.postgres import PostgresAuthority

    authority = PostgresAuthority(
        _app_dsn(admin_dsn, app_role, app_password),
        role="provision-repository-smoke",
        min_size=1,
        max_size=1,
    )
    try:
        authority.open()
        proof = ProductionInstrumentRepository(authority).proof()
        catalogue_ready = (
            proof.active_total > 0
            and proof.nse_equities > 0
            and proof.bse_only_equities > 0
            and proof.indices > 0
            and proof.derivatives == 0
            and proof.out_of_policy_rows == 0
        )
        runtime_control = {}
        for relation in (
            "priority_pipeline_jobs",
            "priority_pipeline_stages",
            "canonical_evidence_snapshots",
            "cross_plane_reconciliation_runs",
            "ml_population_qualification_runs",
            "level5_operational_proof_runs",
        ):
            row = authority.execute(
                f"SELECT count(*) AS row_count FROM runtime_control.{relation}",
                fetch="one",
            )
            runtime_control[relation] = int((row or {}).get("row_count") or 0)
        return {
            "ok": True,
            "state": "REPOSITORY_READY_CATALOGUE_READY" if catalogue_ready else "REPOSITORY_READY_CATALOGUE_EMPTY_OR_INCOMPLETE",
            "catalogue_ready": catalogue_ready,
            "proof": proof.as_dict(),
            "runtime_control": runtime_control,
        }
    finally:
        authority.close()


def export_retired_runtime_evidence(admin_dsn: str, output_path: Path) -> dict:
    """Export forbidden historical PostgreSQL relations outside runtime before removal.

    This is a one-time audit/rollback evidence stream. It never recreates an
    active quarantine table and contains no credentials.
    """
    psycopg, sql = _load_psycopg()
    relations = (
        "reference.option_chain_snapshot",
        "reference.fno_ban_list",
        "integration.legacy_state_quarantine",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    exported = []
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        with psycopg.connect(admin_dsn) as conn:
            with conn.cursor() as cur:
                for relation in relations:
                    cur.execute("SELECT to_regclass(%s)", (relation,))
                    if cur.fetchone()[0] is None:
                        exported.append({"relation": relation, "rows": 0, "present": False})
                        continue
                    schema_name, table_name = relation.split(".", 1)
                    query = sql.SQL("SELECT to_jsonb(row_data) FROM {}.{} AS row_data").format(
                        sql.Identifier(schema_name), sql.Identifier(table_name)
                    )
                    row_count = 0
                    with conn.cursor(name=f"laddu_retired_{table_name}") as stream:
                        stream.itersize = 500
                        stream.execute(query)
                        for (payload,) in stream:
                            handle.write(json.dumps({"relation": relation, "row": payload}, default=str, sort_keys=True) + "\n")
                            row_count += 1
                    exported.append({"relation": relation, "rows": row_count, "present": True})
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return {"ok": True, "path": str(output_path), "sha256": digest, "relations": exported}


def apply_postgres(admin_dsn: str, root: Path, migrations: list[dict], *, app_role: str, app_password: str, database_kind: str, require_parent_applied: bool) -> dict:
    psycopg, sql = _load_psycopg()
    if not migrations:
        raise RuntimeError(f"No {database_kind} PostgreSQL migrations were supplied")
    migration_digests = []
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            for migration in migrations:
                schema_file = root / migration['path']
                ddl = schema_file.read_text(encoding='utf-8')
                digest = hashlib.sha256(schema_file.read_bytes()).hexdigest()
                migration_version = int(migration['version'])
                if schema_file.name != migration['name'] or digest != migration['sha256']:
                    raise RuntimeError(f"MIGRATION_PLAN_SOURCE_MISMATCH:{database_kind}:{migration_version}:{schema_file.name}:{digest}")
                cur.execute("SELECT to_regclass('runtime_control.schema_migrations')")
                migration_table_exists = cur.fetchone()[0] is not None
                existing = None
                if migration_table_exists:
                    cur.execute(
                        "SELECT name,content_sha256 FROM runtime_control.schema_migrations WHERE version=%s",
                        (migration_version,),
                    )
                    existing = cur.fetchone()
                if existing is not None:
                    existing_name, existing_digest = existing
                    if existing_name != schema_file.name or existing_digest != digest:
                        raise RuntimeError(
                            f"MIGRATION_IMMUTABILITY_VIOLATION:{migration_version}:"
                            f"{existing_name}:{existing_digest}:{schema_file.name}:{digest}"
                        )
                    migration_digests.append({
                        "version": migration_version, "name": schema_file.name,
                        "sha256": digest, "state": "already_applied",
                    })
                    continue
                if require_parent_applied and bool(migration.get('parent_required', False)):
                    raise RuntimeError(f"PARENT_MIGRATION_LEDGER_MISSING:{database_kind}:{migration_version}:{schema_file.name}")
                cur.execute(ddl)
                cur.execute(
                    """INSERT INTO runtime_control.schema_migrations(version,name,content_sha256)
                       VALUES(%s,%s,%s)""",
                    (migration_version, schema_file.name, digest),
                )
                migration_digests.append({
                    "version": migration_version, "name": schema_file.name,
                    "sha256": digest, "state": "applied",
                })
            cur.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (app_role,))
            if cur.fetchone() is None:
                cur.execute(
                    sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(
                        sql.Identifier(app_role), sql.Literal(app_password)
                    )
                )
            else:
                cur.execute(
                    sql.SQL('ALTER ROLE {} LOGIN PASSWORD {}').format(
                        sql.Identifier(app_role), sql.Literal(app_password)
                    )
                )
            cur.execute(
                sql.SQL('ALTER ROLE {} SET statement_timeout = {}').format(
                    sql.Identifier(app_role), sql.Literal('2500ms')
                )
            )
            cur.execute(
                sql.SQL('ALTER ROLE {} SET lock_timeout = {}').format(
                    sql.Identifier(app_role), sql.Literal('750ms')
                )
            )
            cur.execute(
                sql.SQL('ALTER ROLE {} SET idle_in_transaction_session_timeout = {}').format(
                    sql.Identifier(app_role), sql.Literal('3000ms')
                )
            )
            if database_kind == 'operational':
                schemas = ['core','trading','risk','accounting','integration','market_data','scanner','runtime_control','reference']
                for schema in schemas:
                    cur.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(app_role)))
                cur.execute(sql.SQL('GRANT SELECT ON runtime_control.schema_migrations TO {}').format(sql.Identifier(app_role)))
                for schema in ['core','trading','risk','accounting','integration','market_data','scanner','reference']:
                    cur.execute(sql.SQL('GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(app_role)))
                    cur.execute(sql.SQL('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(app_role)))
                    cur.execute(sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT, INSERT, UPDATE ON TABLES TO {}').format(sql.Identifier(schema), sql.Identifier(app_role)))
                cur.execute(sql.SQL('GRANT SELECT, INSERT, UPDATE ON runtime_control.kv, runtime_control.daily_learning TO {}').format(sql.Identifier(app_role)))
                cur.execute(sql.SQL('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA runtime_control TO {}').format(sql.Identifier(app_role)))
                cur.execute(sql.SQL('GRANT DELETE ON trading.manual_watch, trading.opportunity_memory, trading.priority_symbols, trading.manual_trade_journal TO {}').format(sql.Identifier(app_role)))
                cur.execute(sql.SQL('GRANT EXECUTE ON FUNCTION integration.claim_outbox(text, integer) TO {}').format(sql.Identifier(app_role)))
                # Immutable audit tables are append-only to the runtime role.
                cur.execute(sql.SQL('REVOKE UPDATE, DELETE ON trading.intent_events, trading.position_events, risk.decisions, accounting.journal_entries, accounting.journal_postings, integration.event_inbox FROM {}').format(sql.Identifier(app_role)))
            else:
                schemas = ['model_registry','research','deployment','runtime_control']
                for schema in schemas:
                    cur.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(app_role)))
                cur.execute(sql.SQL('GRANT SELECT ON runtime_control.schema_migrations TO {}').format(sql.Identifier(app_role)))
                for schema in ['model_registry','research','deployment']:
                    cur.execute(sql.SQL('GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(app_role)))
                    cur.execute(sql.SQL('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(app_role)))
                cur.execute(sql.SQL('GRANT UPDATE ON research.experiments, research.experiment_metrics, research.model_paper_observations, deployment.assignments TO {}').format(sql.Identifier(app_role)))
            required_relations = (
                ('core.instruments','core.securities','core.listings','core.universe_snapshots',
                 'market_data.coverage','market_data.hydration_jobs','scanner.scan_runs',
                 'scanner.scanner_evaluations','trading.canonical_decisions','trading.model_paper_positions',
                 'risk.control_state','accounting.journal_entries','integration.transactional_outbox',
                 'trading.priority_symbols','trading.manual_trade_journal','trading.outcome_learning',
                 'runtime_control.kv','runtime_control.daily_learning','reference.fundamentals_cache')
                if database_kind == 'operational' else
                ('model_registry.models','research.predictions','research.prediction_outcomes',
                 'research.experiments','research.training_publications','research.training_validation_evidence','research.shadow_predictions',
                 'deployment.promotion_decisions','deployment.assignments',
                 'research.selector_populations','research.selector_population_members',
                 'research.selector_arm_predictions','research.selector_outcomes',
                 'research.forward_maturity_checkpoints')
            )
            for relation in required_relations:
                cur.execute('SELECT to_regclass(%s)', (relation,))
                if cur.fetchone()[0] is None:
                    raise RuntimeError(f'REQUIRED_RELATION_MISSING:{relation}')
            if database_kind == 'operational':
                forbidden_relations = (
                    'reference.option_chain_snapshot',
                    'reference.fno_ban_list',
                    'integration.legacy_state_quarantine',
                )
                for forbidden_relation in forbidden_relations:
                    cur.execute('SELECT to_regclass(%s)', (forbidden_relation,))
                    if cur.fetchone()[0] is not None:
                        raise RuntimeError(f'FORBIDDEN_RUNTIME_RELATION_PRESENT:{forbidden_relation}')
                for table in ('trading.manual_watch','trading.opportunity_memory','trading.manual_trade_journal','trading.outcome_learning','trading.model_paper_positions','trading.canonical_decisions'):
                    cur.execute(sql.SQL("SELECT count(*) FROM {} WHERE lower(mode) NOT IN ('intraday','delivery')").format(sql.SQL(table)))
                    forbidden_mode_rows = int(cur.fetchone()[0] or 0)
                    if forbidden_mode_rows:
                        raise RuntimeError(f'FORBIDDEN_MODE_ROWS_PRESENT:{table}:{forbidden_mode_rows}')
            cur.execute('SELECT current_database(), current_setting(\'server_version\')')
            db, version = cur.fetchone()
    aggregate = hashlib.sha256('\n'.join(row['sha256'] for row in migration_digests).encode()).hexdigest()
    result = {'ok': True, 'database': db, 'server_version': version, 'schema_sha256': aggregate, 'migrations': migration_digests, 'role': app_role}
    if database_kind == 'operational':
        result['repository_smoke'] = smoke_operational_repository(admin_dsn, app_role, app_password)
    return result


def apply_questdb(base_url: str, schema_file: Path) -> dict:
    ddl = schema_file.read_text(encoding='utf-8')
    statements = _split_sql(re.sub(r'--.*$', '', ddl, flags=re.M))
    applied = 0
    for statement in statements:
        url = base_url.rstrip('/') + '/exec?' + parse.urlencode({'query': statement})
        with request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode('utf-8') or '{}')
            if response.status != 200 or payload.get('error'):
                raise RuntimeError(f"QuestDB DDL failed: {payload}")
        applied += 1
    check_url = base_url.rstrip('/') + '/exec?' + parse.urlencode({'query': "select table_name from tables() where table_name in ('market_ticks','market_bars','market_data_quality_events')"})
    with request.urlopen(check_url, timeout=5) as response:
        check = json.loads(response.read().decode('utf-8') or '{}')
    return {'ok': True, 'statements': applied, 'tables': check.get('dataset') or []}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--request-file', type=Path)
    parser.add_argument('--operational-admin-dsn')
    parser.add_argument('--governance-admin-dsn')
    parser.add_argument('--operational-app-role', default='laddu_runtime')
    parser.add_argument('--operational-app-password')
    parser.add_argument('--governance-app-role', default='laddu_governance_writer')
    parser.add_argument('--governance-app-password')
    parser.add_argument('--questdb-url')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--require-parent-applied', action='store_true')
    args = parser.parse_args()
    values = vars(args).copy()
    if args.request_file:
        request_path = args.request_file.resolve()
        request_data = json.loads(request_path.read_text(encoding='utf-8-sig'))
        for key, value in request_data.items():
            if key in values and (values.get(key) in (None, '') or key == 'require_parent_applied'):
                values[key] = value
    required = (
        'operational_admin_dsn', 'governance_admin_dsn',
        'operational_app_password', 'governance_app_password',
        'questdb_url', 'root', 'report',
    )
    missing = [name for name in required if values.get(name) in (None, '')]
    if missing:
        parser.error('missing required provisioning values: ' + ', '.join(missing))
    root = Path(values['root']).resolve()
    report_path = Path(values['report']).resolve()
    retired_evidence_path = report_path.parent / 'retired-runtime-evidence.jsonl'
    retired_evidence = export_retired_runtime_evidence(
        values['operational_admin_dsn'], retired_evidence_path
    )
    plan = json.loads((root/'infra/postgres/MIGRATION_PLAN.json').read_text(encoding='utf-8'))
    require_parent_applied = bool(values.get('require_parent_applied', False))
    report = {
        'migration_contract_version': plan['contract_version'],
        'authoritative_parent': plan['authoritative_parent'],
        'require_parent_applied': require_parent_applied,
        'retired_runtime_evidence': retired_evidence,
        'operational': apply_postgres(
            values['operational_admin_dsn'], root, plan['operational'],
            app_role=values['operational_app_role'],
            app_password=values['operational_app_password'],
            database_kind='operational',
            require_parent_applied=require_parent_applied,
        ),
        'governance': apply_postgres(
            values['governance_admin_dsn'], root, plan['governance'],
            app_role=values['governance_app_role'],
            app_password=values['governance_app_password'],
            database_kind='governance',
            require_parent_applied=require_parent_applied,
        ),
        'questdb': (
            {'ok': True, 'state': 'parent_schema_preserved'}
            if require_parent_applied and bool(plan['questdb'].get('parent_required', False))
            else apply_questdb(values['questdb_url'], root/plan['questdb']['path'])
        ),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
