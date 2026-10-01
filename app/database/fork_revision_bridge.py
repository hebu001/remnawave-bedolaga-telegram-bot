"""Strict, offline-from-services bridge for the historical EVO fork revision.

Only PostgreSQL and a frozen, exact schema profile are supported. Nothing in
this module imports models or settings: future models cannot redefine history.
"""

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import inspect, text


PROFILES = Path(__file__).with_name('fork_profiles')
OLD_REVISION = '0106'
NEW_REVISION = 'evo_0106'
AMBIGUOUS_REVISIONS = {f'{n:04d}' for n in range(101, 107)}
SCHEMA_NAME = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


class ForkRevisionError(RuntimeError):
    """Fatal migration ambiguity: never suppressed by startup skip flags."""


class MigrationBusy(ForkRevisionError):
    """Another cooperating migration owns the session lock."""


def _schema(connection, schema):
    if not SCHEMA_NAME.fullmatch(schema):
        raise ForkRevisionError('Invalid schema identifier')
    if connection.dialect.name != 'postgresql':
        raise ForkRevisionError('Revision bridge requires PostgreSQL')
    actual = connection.scalar(text('SELECT current_schema()'))
    if actual != schema:
        raise ForkRevisionError('Selected schema does not match current_schema/search_path')
    return connection.dialect.identifier_preparer.quote_schema(schema)


def schema_digest(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _rows(connection, sql, schema):
    return [dict(row) for row in connection.execute(text(sql), {'schema': schema}).mappings()]


def snapshot_schema(connection, schema='public'):
    """Canonical PostgreSQL catalog, excluding extension-owned objects only.

    No row data, owners, OIDs or dropped-column positions are exported. Foreign
    schema references remain visible; only our selected schema is normalized.
    Constraint/index semantics, defaults, validity, RLS, routines and triggers
    are all part of the fingerprint. alembic_version contents are read separately.
    PostgreSQL 18's ordinary NOT NULL catalog rows are represented by the
    existing column not_null field, after verifying they add no other semantics.
    """
    _schema(connection, schema)
    relation = """c.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname=:schema)
        AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass
          AND d.objid=c.oid AND d.deptype='e')"""
    result = {}
    result['relations'] = _rows(
        connection,
        f"""
        SELECT c.relname AS name,c.relkind AS kind,c.relpersistence AS persistence,
               c.relrowsecurity AS row_security,c.relforcerowsecurity AS force_row_security,
               c.relreplident AS replica_identity,c.reloptions AS options
        FROM pg_class c WHERE {relation} AND c.relkind IN ('r','p','S','v','m','f','c') ORDER BY c.relname
    """,
        schema,
    )
    result['columns'] = _rows(
        connection,
        f"""
        SELECT c.relname AS table_name,a.attname AS name,format_type(a.atttypid,a.atttypmod) AS type,
               a.attnotnull AS not_null,a.attidentity AS identity,a.attgenerated AS generated,
               pg_get_expr(ad.adbin,ad.adrelid) AS default,
               CASE WHEN a.attcollation=0 THEN NULL ELSE a.attcollation::regcollation::text END AS collation
        FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
        LEFT JOIN pg_attrdef ad ON ad.adrelid=c.oid AND ad.adnum=a.attnum
        WHERE {relation} AND c.relkind IN ('r','p','v','m','f') AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.relname,a.attname
    """,
        schema,
    )
    # PG18 records NOT NULL in both catalogs. Remove only a proven ordinary
    # mirror; IS TRUE keeps missing/unknown metadata visible. JSON field access
    # lets this same query run on PG15, which lacks conenforced/conperiod.
    result['constraints'] = _rows(
        connection,
        f"""
        SELECT c.relname AS table_name,CASE WHEN co.contype='f' THEN NULL ELSE co.conname END AS name,
               co.contype AS type,pg_get_constraintdef(co.oid,true) AS definition,
               co.condeferrable AS deferrable,co.condeferred AS deferred,co.convalidated AS validated,
               CASE WHEN co.confrelid=0 THEN NULL ELSE rn.nspname END AS referenced_schema
        FROM pg_class c JOIN pg_constraint co ON co.conrelid=c.oid
        LEFT JOIN pg_class rc ON rc.oid=co.confrelid LEFT JOIN pg_namespace rn ON rn.oid=rc.relnamespace
        LEFT JOIN pg_attribute a ON co.contype='n' AND a.attrelid=c.oid AND a.attnum=co.conkey[1]
        WHERE {relation} AND NOT ((
          co.contype='n' AND c.relkind IN ('r','p') AND co.connamespace=c.relnamespace
          AND a.attnum>0 AND NOT a.attisdropped AND a.attnotnull AND co.conkey=ARRAY[a.attnum]
          AND co.convalidated AND NOT co.condeferrable AND NOT co.condeferred
          AND to_jsonb(co)->>'conenforced'='true' AND to_jsonb(co)->>'conperiod'='false'
          AND co.conislocal AND co.coninhcount=0 AND NOT co.connoinherit AND co.conparentid=0
          AND co.contypid=0 AND co.conindid=0 AND co.confrelid=0 AND co.conbin IS NULL
          AND co.confupdtype=' ' AND co.confdeltype=' ' AND co.confmatchtype=' '
          AND co.confkey IS NULL AND co.conpfeqop IS NULL AND co.conppeqop IS NULL
          AND co.conffeqop IS NULL AND co.confdelsetcols IS NULL AND co.conexclop IS NULL
          AND pg_get_constraintdef(co.oid,true)='NOT NULL ' || quote_ident(a.attname)
          AND NOT EXISTS (SELECT 1 FROM pg_constraint other WHERE other.oid<>co.oid
            AND other.conrelid=co.conrelid AND other.contype='n' AND other.conkey=co.conkey)
        ) IS TRUE)
        ORDER BY c.relname,co.contype,pg_get_constraintdef(co.oid,true)
    """,
        schema,
    )
    result['indexes'] = _rows(
        connection,
        f"""
        SELECT c.relname AS table_name,ic.relname AS name,am.amname AS method,
               pg_get_indexdef(i.indexrelid) AS definition,i.indisunique AS unique,
               i.indisprimary AS primary,i.indisvalid AS valid,i.indisready AS ready,
               i.indisreplident AS replica_identity,i.indisclustered AS clustered
        FROM pg_class c JOIN pg_index i ON i.indrelid=c.oid JOIN pg_class ic ON ic.oid=i.indexrelid
        JOIN pg_am am ON am.oid=ic.relam WHERE {relation} ORDER BY c.relname,ic.relname
    """,
        schema,
    )
    result['triggers'] = _rows(
        connection,
        f"""
        SELECT c.relname AS table_name,t.tgname AS name,t.tgenabled AS enabled,
               pg_get_triggerdef(t.oid,true) AS definition
        FROM pg_class c JOIN pg_trigger t ON t.tgrelid=c.oid
        WHERE {relation} AND NOT t.tgisinternal ORDER BY c.relname,t.tgname
    """,
        schema,
    )
    result['sequences'] = _rows(
        connection,
        f"""
        SELECT c.relname AS name,format_type(s.seqtypid,NULL) AS type,s.seqstart AS start,
               s.seqincrement AS increment,s.seqmax AS max,s.seqmin AS min,s.seqcache AS cache,s.seqcycle AS cycle,
               tc.relname AS owned_table,a.attname AS owned_column
        FROM pg_class c JOIN pg_sequence s ON s.seqrelid=c.oid
        LEFT JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype IN ('a','i')
        LEFT JOIN pg_class tc ON tc.oid=d.refobjid LEFT JOIN pg_attribute a ON a.attrelid=tc.oid AND a.attnum=d.refobjsubid
        WHERE {relation} ORDER BY c.relname
    """,
        schema,
    )
    result['routines'] = _rows(
        connection,
        """
        SELECT p.proname AS name,pg_get_function_identity_arguments(p.oid) AS arguments,
               pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname=:schema AND p.prokind IN ('f','p') AND NOT EXISTS
          (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e')
        ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)
    """,
        schema,
    )
    result['types'] = _rows(
        connection,
        """
        SELECT t.typname AS name,t.typtype AS type,format_type(t.typbasetype,t.typtypmod) AS base_type,
               t.typnotnull AS not_null,t.typdefault AS default,
               ARRAY(SELECT e.enumlabel FROM pg_enum e WHERE e.enumtypid=t.oid ORDER BY e.enumsortorder) AS labels
        FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname=:schema AND t.typtype IN ('d','e','r','m') AND NOT EXISTS
          (SELECT 1 FROM pg_depend d WHERE d.classid='pg_type'::regclass AND d.objid=t.oid AND d.deptype='e')
        ORDER BY t.typname
    """,
        schema,
    )
    result['policies'] = _rows(
        connection,
        f"""
        SELECT c.relname AS table_name,p.polname AS name,p.polcmd AS command,p.polpermissive AS permissive,
               pg_get_expr(p.polqual,p.polrelid) AS using,pg_get_expr(p.polwithcheck,p.polrelid) AS check
        FROM pg_class c JOIN pg_policy p ON p.polrelid=c.oid WHERE {relation} ORDER BY c.relname,p.polname
    """,
        schema,
    )
    result['rules'] = _rows(
        connection,
        f"""
        SELECT c.relname AS table_name,r.rulename AS name,r.ev_enabled AS enabled,
               pg_get_ruledef(r.oid,true) AS definition
        FROM pg_class c JOIN pg_rewrite r ON r.ev_class=c.oid
        WHERE {relation} ORDER BY c.relname,r.rulename
    """,
        schema,
    )
    # Unsupported schema-local object families must be absent, not invisible.
    result['other_objects'] = _rows(
        connection,
        """
        WITH objects AS (
          SELECT 'pg_proc'::regclass AS classid,p.oid AS objid,'aggregate' AS kind,p.proname AS name FROM pg_proc p
            JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=:schema AND p.prokind='a'
          UNION ALL SELECT 'pg_operator'::regclass,o.oid,'operator',o.oprname FROM pg_operator o JOIN pg_namespace n ON n.oid=o.oprnamespace WHERE n.nspname=:schema
          UNION ALL SELECT 'pg_collation'::regclass,c.oid,'collation',c.collname FROM pg_collation c JOIN pg_namespace n ON n.oid=c.collnamespace WHERE n.nspname=:schema
          UNION ALL SELECT 'pg_conversion'::regclass,c.oid,'conversion',c.conname FROM pg_conversion c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname=:schema
          UNION ALL SELECT 'pg_opclass'::regclass,c.oid,'opclass',c.opcname FROM pg_opclass c JOIN pg_namespace n ON n.oid=c.opcnamespace WHERE n.nspname=:schema
          UNION ALL SELECT 'pg_opfamily'::regclass,c.oid,'opfamily',c.opfname FROM pg_opfamily c JOIN pg_namespace n ON n.oid=c.opfnamespace WHERE n.nspname=:schema
          UNION ALL SELECT 'pg_ts_config'::regclass,c.oid,'text_search_configuration',c.cfgname FROM pg_ts_config c JOIN pg_namespace n ON n.oid=c.cfgnamespace WHERE n.nspname=:schema
          UNION ALL SELECT 'pg_ts_dict'::regclass,c.oid,'text_search_dictionary',c.dictname FROM pg_ts_dict c JOIN pg_namespace n ON n.oid=c.dictnamespace WHERE n.nspname=:schema
        )
        SELECT kind,name FROM objects o WHERE NOT EXISTS (
          SELECT 1 FROM pg_depend d WHERE d.classid=o.classid AND d.objid=o.objid AND d.deptype='e'
        ) ORDER BY kind,name
    """,
        schema,
    )

    def normalize(value):
        if isinstance(value, bytes):
            return value.decode().rstrip('\0')
        if isinstance(value, str):
            if value == schema:
                return '<schema>'
            return value.replace(f'"{schema}".', '<schema>.').replace(f'{schema}.', '<schema>.')
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value

    return normalize(result)


def read_revisions(connection, schema='public'):
    quoted = _schema(connection, schema)
    if not inspect(connection).has_table('alembic_version', schema=schema):
        return None
    return sorted(connection.scalars(text(f'SELECT version_num FROM {quoted}.alembic_version')).all())


@dataclass(frozen=True)
class BridgeReport:
    state: str
    revisions: list[str] | None
    profile: str | None
    schema_sha256: str
    proposed_revision: str | None
    differences: list[str]
    applied: bool = False

    def to_dict(self):
        return asdict(self)


def inspect_bridge(connection, schema='public'):
    revisions = read_revisions(connection, schema)
    snapshot = snapshot_schema(connection, schema)
    digest = schema_digest(snapshot)
    closest = None
    differences = []
    for path in sorted(PROFILES.glob('*.json')):
        profile = json.loads(path.read_text())
        expected = profile['schema']
        if schema_digest(expected) != profile['schema_sha256']:
            raise ForkRevisionError('Frozen fork profile checksum mismatch')
        delta = [
            section
            for section in sorted(expected.keys() | snapshot.keys())
            if expected.get(section) != snapshot.get(section)
        ]
        if closest is None or len(delta) < len(differences):
            closest, differences = profile['name'], delta
        if not delta:
            state = {('0106',): 'bridge_required', ('evo_0106',): 'already_bridged'}.get(
                tuple(revisions or ()), 'unsupported_revision'
            )
            return BridgeReport(
                state, revisions, profile['name'], digest, NEW_REVISION if state != 'unsupported_revision' else None, []
            )
    return BridgeReport('unrecognized_schema', revisions, None, digest, None, differences or ['no_frozen_profiles'])


def _lock_key(schema):
    return int.from_bytes(hashlib.sha256(f'evo-alembic:{schema}'.encode()).digest()[:8], 'big', signed=True)


@contextmanager
def migration_lock(connection, schema='public'):
    """Session lock survives Alembic autocommit blocks; fail immediately if busy."""
    if connection.dialect.name != 'postgresql':
        yield
        return
    key = _lock_key(schema)
    held = connection.info.setdefault('evo_migration_locks', set())
    if key in held:
        yield
        return
    if connection.in_transaction():
        raise ForkRevisionError('Migration entrypoint requires a connection without an active transaction')
    _schema(connection, schema)
    connection.rollback()
    acquired = connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key})
    connection.commit()
    if not acquired:
        raise MigrationBusy('Another migration holds the schema lock; retry later')
    held.add(key)
    try:
        yield
    finally:
        if connection.in_transaction():
            connection.rollback()
        connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
        connection.commit()
        held.remove(key)


def bridge_revision(connection, *, schema='public', apply=False, expected_revision=None, expected_digest=None):
    """Dry-run by default. Apply requires the exact digest from a prior dry-run."""
    if apply and (expected_revision != OLD_REVISION or not expected_digest):
        raise ForkRevisionError('Apply requires --expected-revision 0106 and --expected-digest from dry-run')
    with migration_lock(connection, schema):
        with connection.begin():
            if not apply:
                connection.execute(text('SET TRANSACTION READ ONLY'))
            connection.execute(text("SET LOCAL lock_timeout = '3s'"))
            connection.execute(text("SET LOCAL statement_timeout = '30s'"))
            quoted = _schema(connection, schema)
            names = inspect(connection).get_table_names(schema=schema)
            if apply and 'alembic_version' in names:
                connection.execute(text(f'LOCK TABLE {quoted}.alembic_version IN ACCESS EXCLUSIVE MODE'))
                tables = [
                    f'{quoted}.{connection.dialect.identifier_preparer.quote(name)}'
                    for name in sorted(names)
                    if name != 'alembic_version'
                ]
                if tables:
                    connection.execute(text(f'LOCK TABLE {", ".join(tables)} IN SHARE MODE'))
            report = inspect_bridge(connection, schema)
            if not apply:
                return report
            if report.state not in {'bridge_required', 'already_bridged'}:
                raise ForkRevisionError(
                    f'Bridge rejected: {report.state}; schema sections differ: {report.differences}'
                )
            if report.schema_sha256 != expected_digest:
                raise ForkRevisionError('Schema changed since dry-run; inspect again')
            if report.state == 'already_bridged':
                return report
            result = connection.execute(
                text(f'UPDATE {quoted}.alembic_version SET version_num=:new WHERE version_num=:old'),
                {'new': NEW_REVISION, 'old': OLD_REVISION},
            )
            if result.rowcount != 1 or read_revisions(connection, schema) != [NEW_REVISION]:
                raise ForkRevisionError('Revision rewrite did not affect exactly one expected row')
            if schema_digest(snapshot_schema(connection, schema)) != report.schema_sha256:
                raise ForkRevisionError('Schema changed during bridge')
            return BridgeReport(**{**report.to_dict(), 'applied': True})


def migration_preflight(connection, schema='public', *, allow_bootstrap=False, known_revisions=None):
    """Refuse ambiguous/unknown history before Alembic's revision planner runs."""
    if connection.dialect.name != 'postgresql':
        # The bridge only supports PostgreSQL; do not reinterpret an old SQLite fork.
        inspector = inspect(connection)
        if inspector.has_table('alembic_version'):
            revisions = set(connection.scalars(text('SELECT version_num FROM alembic_version')))
            if revisions & AMBIGUOUS_REVISIONS:
                raise ForkRevisionError('Ambiguous old fork revision; PostgreSQL bridge required')
            known = known_revisions or ({f'{n:04d}' for n in range(1, 101)} | {f'evo_{n:04d}' for n in range(101, 107)})
            if not revisions or not revisions <= known or (len(revisions) > 1 and known_revisions is None):
                raise ForkRevisionError('Unknown or empty database revision for this checkout')
        elif not allow_bootstrap and (inspector.get_table_names() or inspector.get_view_names()):
            raise ForkRevisionError('Nonempty schema without revision; refusing automatic bootstrap/stamp')
        return
    revisions = read_revisions(connection, schema)
    if revisions is not None:
        combined_resume = (
            NEW_REVISION in revisions and known_revisions is not None and set(revisions) <= known_revisions
        )
        if not revisions or (any(revision in AMBIGUOUS_REVISIONS for revision in revisions) and not combined_resume):
            raise ForkRevisionError(
                'Ambiguous fork revision: run python scripts/bridge_fork_revision.py (dry-run first)'
            )
        known = known_revisions or ({f'{n:04d}' for n in range(1, 101)} | {f'evo_{n:04d}' for n in range(101, 107)})
        if any(revision not in known for revision in revisions):
            raise ForkRevisionError('Unknown database revision for this checkout')
        if len(revisions) > 1 and known_revisions is None:
            raise ForkRevisionError('Multiple database revisions are unsupported by this fork checkout')
        return
    if allow_bootstrap:
        return
    snapshot = snapshot_schema(connection, schema)
    if any(snapshot.values()):
        raise ForkRevisionError('Nonempty schema without revision; refusing automatic bootstrap/stamp')
