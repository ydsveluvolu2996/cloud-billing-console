"""Installed, hash-pinned interpreter for one narrowly additive migration.

Candidate migration files are parsed as data, never imported. Version 1 supports
one nullable DateTimeField on the existing saved-report table per release.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


MIGRATION_NAME = re.compile(r'[0-9]{4}_[a-z][a-z0-9_]{0,99}')
IDENTIFIER = re.compile(r'[a-z][a-z0-9_]{0,62}')
TABLES = {'savedreport': 'billing_savedreport'}
DB_CONTAINER = 'cloud-billing-db-1'
NETWORK = 'cloud-billing_database'
CONFIG_PATH = Path('/etc/cloud-billing/release.json')
# A private per-container tmpfs, never shared host temporary storage.
CLIENT_TMPFS = '/tmp:rw,nosuid,nodev,size=16m'  # nosec B108


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def protected(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'Missing or linked maintenance file')
    for item in (path, *path.parents):
        info = item.lstat()
        require(not item.is_symlink() and info.st_uid == 0 and not info.st_mode & 0o022,
                'Maintenance files must be root-owned and not writable by other users')


def canonical_plan(path, expected):
    """Accept precisely the generated literal form; no Python is evaluated."""
    path = Path(path)
    name = path.stem
    require(MIGRATION_NAME.fullmatch(name), 'Unsupported migration filename')
    prior = sorted(Path(item).stem for item in expected
                   if item.startswith('billing/migrations/') and Path(item).name != '__init__.py')
    require(prior and all(MIGRATION_NAME.fullmatch(item) for item in prior), 'Unsupported installed migration history')
    require(int(name[:4]) == int(prior[-1][:4]) + 1, 'Migration must directly follow the installed history')
    require(path.stat().st_size <= 8192, 'Migration exceeds the declarative size limit')
    tree = ast.parse(path.read_text())
    require(len(tree.body) == 2, 'Migration contains extra Python statements')
    imports, migration = tree.body
    require(isinstance(imports, ast.ImportFrom) and imports.module == 'django.db' and imports.level == 0
            and [(item.name, item.asname) for item in imports.names] == [('migrations', None), ('models', None)],
            'Only the canonical Django migration imports are allowed')
    require(isinstance(migration, ast.ClassDef) and migration.name == 'Migration'
            and not migration.decorator_list and not migration.keywords and len(migration.bases) == 1
            and ast.dump(migration.bases[0]) == ast.dump(ast.parse('migrations.Migration', mode='eval').body)
            and not getattr(migration, 'type_params', []), 'Unsupported migration class')
    require(len(migration.body) == 2, 'Migration contains extra class statements')
    values = {}
    for statement in migration.body:
        require(isinstance(statement, ast.Assign) and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name), 'Migration must contain literal assignments')
        key = statement.targets[0].id
        require(key in ('dependencies', 'operations') and key not in values, 'Unexpected migration assignment')
        values[key] = statement.value
    require(set(values) == {'dependencies', 'operations'}, 'Migration assignments are incomplete')
    require(ast.dump(values['dependencies']) == ast.dump(ast.parse(repr([('billing', prior[-1])]), mode='eval').body),
            'Migration must depend only on the installed billing leaf')
    operations = values['operations']
    require(isinstance(operations, ast.List) and len(operations.elts) == 1, 'Exactly one AddField is supported')
    operation = operations.elts[0]
    require(isinstance(operation, ast.Call) and not operation.args
            and ast.dump(operation.func) == ast.dump(ast.parse('migrations.AddField', mode='eval').body),
            'Only canonical AddField is supported')
    fields = {item.arg: item.value for item in operation.keywords}
    require(len(operation.keywords) == 3 and set(fields) == {'model_name', 'name', 'field'},
            'AddField options are outside the automatic migration policy')
    require(all(isinstance(fields[key], ast.Constant) and type(fields[key].value) is str
                for key in ('model_name', 'name')), 'Model and field names must be literal strings')
    model, column = fields['model_name'].value, fields['name'].value
    require(model in TABLES and IDENTIFIER.fullmatch(column), 'Table or column is outside the automatic migration policy')
    field = fields['field']
    require(isinstance(field, ast.Call) and not field.args
            and ast.dump(field.func) == ast.dump(ast.parse('models.DateTimeField', mode='eval').body),
            'Only nullable DateTimeField is supported')
    options = {item.arg: item.value for item in field.keywords}
    require(len(options) == len(field.keywords) and set(options) <= {'blank', 'null'} and 'null' in options,
            'Field defaults, indexes, constraints and custom options are not supported')
    require(all(isinstance(value, ast.Constant) and type(value.value) is bool for value in options.values())
            and options['null'].value is True, 'Field must be nullable with literal boolean options')
    return {'version': 1, 'path': 'billing/migrations/' + path.name, 'sha256': digest(path),
            'name': name, 'dependency': prior[-1], 'prior': prior, 'table': TABLES[model], 'column': column}


def plan_addition(source, expected, actual):
    require(all(actual.get(name) == checksum for name, checksum in expected.items()),
            'An installed migration, policy or service configuration changed; separate maintenance is required')
    added = set(actual) - set(expected)
    require(len(added) == 1, 'Only one new additive migration is supported per release')
    name = added.pop()
    require(name.startswith('billing/migrations/'), 'New protected configuration requires separate maintenance')
    return canonical_plan(Path(source) / name, expected)


def sql_for(plan):
    """Build fixed SQL from an already validated, bounded declarative plan."""
    table, column, name = plan['table'], plan['column'], plan['name']
    require(table in TABLES.values() and IDENTIFIER.fullmatch(column) and MIGRATION_NAME.fullmatch(name),
            'Unsafe declarative migration identifier')
    require(plan['version'] == 1 and plan['prior'] and plan['dependency'] == plan['prior'][-1]
            and all(MIGRATION_NAME.fullmatch(item) for item in plan['prior']), 'Unsafe migration history')
    prior_json = json.dumps(plan['prior'])
    # The identifiers and literal names above cannot contain SQL quotes. All
    # executable SQL below belongs to the installed helper, never the artifact.
    access = f"""SELECT jsonb_build_object(
      'table', (SELECT jsonb_build_object('owner',pg_get_userbyid(relowner),'rls',relrowsecurity,
                  'force',relforcerowsecurity,'acl',relacl::text) FROM pg_class WHERE oid='public.{table}'::regclass),
      'policies', (SELECT jsonb_agg(to_jsonb(p) ORDER BY policyname) FROM pg_policies p
                   WHERE schemaname='public' AND tablename='{table}'),
      'columns', (SELECT jsonb_agg(jsonb_build_array(attname,attacl::text) ORDER BY attnum) FROM pg_attribute
                   WHERE attrelid='public.{table}'::regclass AND attnum>0 AND NOT attisdropped AND attname<>'{column}'),
      'roles', (SELECT jsonb_agg(jsonb_build_array(rolname,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolinherit)
                  ORDER BY rolname) FROM pg_roles WHERE rolname IN ('billing_web','billing_collector')),
      'web', jsonb_build_array(has_table_privilege('billing_web','public.{table}','SELECT'),
               has_table_privilege('billing_web','public.{table}','INSERT'),
               has_table_privilege('billing_web','public.{table}','UPDATE'),
               has_table_privilege('billing_web','public.{table}','DELETE')),
      'collector', jsonb_build_array(has_table_privilege('billing_collector','public.{table}','SELECT'),
               has_table_privilege('billing_collector','public.{table}','INSERT'),
               has_table_privilege('billing_collector','public.{table}','UPDATE'),
               has_table_privilege('billing_collector','public.{table}','DELETE')))
    """
    rows = f"""SELECT count(*),md5(coalesce(string_agg(md5((to_jsonb(t)-'{column}')::text),'' ORDER BY id),''))
               FROM public.\"{table}\" t"""
    return f"""\\set ON_ERROR_STOP on
SET search_path=pg_catalog,public;
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
SET LOCAL idle_in_transaction_session_timeout='10s';
DO $fixed_additive_migration$
DECLARE
  old_access jsonb; new_access jsonb; history jsonb; definition jsonb;
  old_count bigint; new_count bigint; old_rows text; new_rows text; applied_count integer;
BEGIN
  IF current_user <> 'billing_admin' OR NOT EXISTS
       (SELECT 1 FROM pg_stat_ssl WHERE pid=pg_backend_pid() AND ssl) THEN
    RAISE EXCEPTION 'Expected the existing TLS administration connection';
  END IF;
  LOCK TABLE public.\"{table}\", public.django_migrations IN ACCESS EXCLUSIVE MODE;
  {access} INTO old_access;
  IF old_access->'table'->>'owner' <> 'billing_admin'
     OR old_access->'table'->'rls' IS DISTINCT FROM 'true'::jsonb
     OR old_access->'policies' IS NULL OR jsonb_array_length(old_access->'policies') = 0
     OR old_access->'web' IS DISTINCT FROM '[true,true,true,true]'::jsonb
     OR old_access->'collector' IS DISTINCT FROM '[true,false,false,false]'::jsonb
     OR old_access->'roles' IS DISTINCT FROM
        '[["billing_collector",false,false,false,false,false],["billing_web",false,false,false,false,false]]'::jsonb THEN
    RAISE EXCEPTION 'Existing table ownership or access controls differ';
  END IF;
  SELECT coalesce(jsonb_agg(name ORDER BY name),'[]'::jsonb) INTO history
    FROM public.django_migrations WHERE app='billing' AND name<>'{name}';
  IF history IS DISTINCT FROM '{prior_json}'::jsonb THEN
    RAISE EXCEPTION 'Installed database migration history differs';
  END IF;
  SELECT count(*) INTO applied_count FROM public.django_migrations WHERE app='billing' AND name='{name}';
  IF applied_count NOT IN (0,1) THEN RAISE EXCEPTION 'Duplicate migration record'; END IF;
  {rows} INTO old_count,old_rows;
  IF applied_count=0 THEN
    IF EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid='public.{table}'::regclass
              AND attname='{column}' AND NOT attisdropped) THEN
      RAISE EXCEPTION 'An unrecorded target column already exists';
    END IF;
    ALTER TABLE public.\"{table}\" ADD COLUMN \"{column}\" timestamp with time zone NULL;
    INSERT INTO public.django_migrations(app,name,applied) VALUES('billing','{name}',CURRENT_TIMESTAMP);
    IF EXISTS(SELECT 1 FROM public.\"{table}\" WHERE \"{column}\" IS NOT NULL) THEN
      RAISE EXCEPTION 'A new nullable column unexpectedly has values';
    END IF;
  END IF;
  SELECT jsonb_build_array(a.atttypid='pg_catalog.timestamptz'::regtype,a.atttypmod,a.attnotnull,
                          a.atthasdef,a.attidentity,a.attgenerated,a.attacl IS NULL,
                          NOT EXISTS(SELECT 1 FROM pg_depend d
                            WHERE d.refclassid='pg_class'::regclass AND d.refobjid=a.attrelid
                              AND d.refobjsubid=a.attnum
                              AND d.classid IN ('pg_class'::regclass,'pg_constraint'::regclass)))
    INTO definition FROM pg_attribute a WHERE a.attrelid='public.{table}'::regclass
     AND a.attname='{column}' AND NOT a.attisdropped;
  IF definition IS DISTINCT FROM '[true,-1,false,false,"","",true,true]'::jsonb THEN
    RAISE EXCEPTION 'Target column does not match nullable DateTimeField';
  END IF;
  {access} INTO new_access;
  {rows} INTO new_count,new_rows;
  IF new_access IS DISTINCT FROM old_access OR new_count<>old_count OR new_rows<>old_rows THEN
    RAISE EXCEPTION 'Existing rows or access controls changed';
  END IF;
END $fixed_additive_migration$;
COMMIT;
SELECT json_build_object('migration','{name}','verified',true);
"""


def container_id(run, name):
    output = run(['docker','container','ls','--all','--no-trunc','--filter','name='+name,
                  '--format','{{.ID}} {{.Names}}'], timeout=15)
    matches = [row[0] for line in output.splitlines() if len(row := line.split()) == 2 and row[1] == name]
    require(len(matches) <= 1 and all(re.fullmatch('[a-f0-9]{64}', item) for item in matches),
            'Unexpected migration container identity')
    return matches[0] if matches else None


def stop_container(run, name):
    identity = container_id(run, name)
    if identity is None:
        return
    for args in (['docker','stop','--time','10',identity], ['docker','wait',identity]):
        try:
            run(args, timeout=20)
        except (subprocess.SubprocessError, OSError):
            pass  # The --rm container may disappear between either operation.
    remaining = container_id(run, name)
    if remaining is not None:
        require(remaining == identity, 'Migration container identity changed')
        state = json.loads(run(['docker','inspect','--type','container',identity], timeout=15))[0]['State']
        require(not state['Running'] and not state['Restarting'],
                'Migration cleanup unconfirmed; inspect the named migration container before retrying')


def execute_sql(root, release_id, plan, run):
    password = root / '.deployment/secrets/admin_db_password'
    ca = root / '.deployment/postgres-tls/ca.crt'
    protected(password)
    protected(ca)
    database = json.loads(run(['docker','inspect','--type','container',DB_CONTAINER], timeout=15))[0]
    require(database['State']['Running'] and NETWORK in database['NetworkSettings']['Networks']
            and re.fullmatch('sha256:[a-f0-9]{64}', database['Image']), 'Unexpected current database container')
    name = 'billing-additive-' + release_id
    require(re.fullmatch('billing-additive-[a-f0-9]{40}-[0-9]+-[0-9]+',name), 'Invalid migration container name')
    require(container_id(run, name) is None, 'A prior migration container still exists; inspect before retrying')
    args = ['docker','run','--name',name,'--rm','-i','--user','0','--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges:true','--pids-limit','32','--memory','128m',
            '--network',NETWORK,'--ip','172.30.50.254','--tmpfs',CLIENT_TMPFS,
            '--mount',f'type=bind,src={password},dst=/run/secrets/admin_db_password,readonly',
            '--mount',f'type=bind,src={ca},dst=/run/secrets/ca.crt,readonly',
            '-e','PGSSLMODE=verify-full','-e','PGSSLROOTCERT=/run/secrets/ca.crt','-e','PGCONNECT_TIMEOUT=10',
            '--entrypoint','sh',database['Image'],'-c',
            'export PGPASSWORD="$(cat /run/secrets/admin_db_password)"; exec psql -X -qAt -v ON_ERROR_STOP=1 -h db -U billing_admin -d billing']
    try:
        output = run(args, input=sql_for(plan), timeout=120)
    finally:
        stop_container(run, name)
    result = json.loads(output.strip())
    require(result == {'migration':plan['name'],'verified':True}, 'Database migration verification receipt differs')
    return result


def apply(root, stage, release_id, plan, config, run, write_json, config_path=CONFIG_PATH):
    """Recoverable expand-only schema step; caller holds the deployment lock."""
    root, stage, config_path = Path(root), Path(stage), Path(config_path)
    require(config['runtime'] == 'combined', 'Automatic additive migrations require the combined runtime')
    protected(config_path)
    old_config = json.loads(config_path.read_text())
    require(old_config == config, 'Installed release configuration changed; retry from its current baseline')
    source = stage / 'source'
    target = root / plan['path']
    require(digest(source / plan['path']) == plan['sha256'], 'Staged migration contents changed')
    base = {name: checksum for name,checksum in config['compatibility'].items() if name != plan['path']}
    require(canonical_plan(source / plan['path'],base) == plan, 'Stored migration plan differs from the source')
    require(config['compatibility'].get(plan['path']) in (None,plan['sha256']), 'Installed migration baseline differs')
    for name, checksum in base.items():
        protected(root / name)
        require(digest(root / name) == checksum and digest(source / name) == checksum,
                'An installed protected file changed before migration')
    journal = stage / 'additive-migration.json'
    if journal.exists():
        state = json.loads(journal.read_text())
        require(state['plan'] == plan and state['release_id'] == release_id, 'Migration journal differs')
    else:
        state = {'release_id':release_id,'plan':plan,'phase':'prepared','before_config':old_config}
        write_json(journal,state)
    if state['phase'] == 'complete':
        protected(target)
        require(config['compatibility'].get(plan['path']) == plan['sha256'] and digest(target) == plan['sha256'],
                'Completed migration baseline differs')
        return state
    if 'backup_stamp' not in state:
        output = run(['bash',str(root / 'deploy/backup.sh')], cwd=root, timeout=300)
        match = re.search(r'Backup validated and uploaded at ([0-9]{8}T[0-9]{6}Z)',output)
        require(match is not None, 'Encrypted off-server backup receipt is absent; schema unchanged')
        state.update(backup_stamp=match[1],phase='backed-up')
        write_json(journal,state)
    # Repeat this fixed verification transaction after any ambiguous interruption.
    # An existing exact record/column is verified without modifying report data.
    state['database'] = execute_sql(root,release_id,plan,run)
    state['phase'] = 'database-verified'
    write_json(journal,state)
    require(json.loads(config_path.read_text()) == old_config, 'Installed baseline changed during migration')
    if target.exists():
        protected(target)
        require(digest(target) == plan['sha256'], 'Existing migration file differs')
    else:
        temporary = target.with_suffix('.additive-new')
        if temporary.exists():
            # An interrupted local copy is safe to replace from the reverified
            # artifact, but a linked or non-root-owned file is never followed.
            protected(temporary)
        with temporary.open('wb') as stream:
            stream.write((source / plan['path']).read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        temporary.replace(target)
    descriptor = os.open(target.parent,os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    updated = {**old_config,'compatibility':{**old_config['compatibility'],plan['path']:plan['sha256']}}
    write_json(config_path,updated)
    config.clear()
    config.update(updated)
    state['phase'] = 'complete'
    write_json(journal,state)
    return state
