import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(DIRECTORY))
import additive_migrations as additive
import agent

NAME = '0022_savedreport_archived_at'
PATH = 'billing/migrations/' + NAME + '.py'
PREVIOUS = 'billing/migrations/0021_activationrequest.py'
RELEASE = 'a'*40 + '-123-1'
CANONICAL = """from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = [('billing', '0021_activationrequest')]
    operations = [migrations.AddField(model_name='savedreport', name='archived_at',
                  field=models.DateTimeField(blank=True, null=True))]
"""


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)
        self.path = self.source / PATH
        self.path.parent.mkdir(parents=True)
        self.path.write_text(CANONICAL)
        self.expected = {PREVIOUS:'a'*64, 'billing/migrations/__init__.py':'b'*64, 'compose.yaml':'c'*64}

    def test_current_canonical_migration_is_data_only_plan(self):
        plan = additive.canonical_plan(self.path,self.expected)
        self.assertEqual((plan['name'],plan['table'],plan['column']),
                         (NAME,'billing_savedreport','archived_at'))
        self.assertEqual(plan['prior'],['0021_activationrequest'])
        self.assertEqual(plan['sha256'],hashlib.sha256(CANONICAL.encode()).hexdigest())

    def test_executable_or_non_additive_forms_are_rejected(self):
        sources = [
            CANONICAL + "\nopen('/tmp/should-never-exist','w')\n",
            CANONICAL.replace('from django.db import migrations, models','import os\nfrom django.db import migrations, models'),
            CANONICAL.replace('migrations, models','migrations, models as x'),
            CANONICAL.replace('class Migration','@evil()\nclass Migration'),
            CANONICAL.replace('migrations.Migration','evil()'),
            CANONICAL.replace('    operations =','    def evil(self): pass\n    operations ='),
            CANONICAL.replace('migrations.AddField','migrations.RunSQL'),
            CANONICAL.replace('migrations.AddField','migrations.RunPython'),
            CANONICAL.replace('null=True','null=False'),
            CANONICAL.replace('null=True','null=bool(1)'),
            CANONICAL.replace('null=True','null=True, default=None'),
            CANONICAL.replace('null=True','null=True, db_index=True'),
            CANONICAL.replace('null=True','null=True, unique=True'),
            CANONICAL.replace('null=True','null=True, db_column="elsewhere"'),
            CANONICAL.replace('null=True','null=True, **options'),
            CANONICAL.replace('null=True','null=True, null=True'),
            CANONICAL.replace('models.DateTimeField','custom.DateTimeField'),
            CANONICAL.replace('models.DateTimeField','models.TextField'),
            CANONICAL.replace("model_name='savedreport'","model_name='user'"),
            CANONICAL.replace("name='archived_at'","name='x; DROP TABLE auth_user'"),
            CANONICAL.replace("name='archived_at'","name=evil()"),
            CANONICAL.replace("'0021_activationrequest'","'0020_earlier'"),
            CANONICAL.replace("('billing', '0021_activationrequest')","('auth', '0021_activationrequest')"),
        ]
        for source in sources:
            with self.subTest(source=source):
                self.path.write_text(source)
                with self.assertRaises((ValueError,SyntaxError)):
                    additive.canonical_plan(self.path,self.expected)

    def test_new_file_cannot_bypass_old_protected_hashes(self):
        actual = {**self.expected,PATH:additive.digest(self.path)}
        self.assertEqual(additive.plan_addition(self.source,self.expected,actual)['name'],NAME)
        for name in self.expected:
            altered = {**actual,name:'f'*64}
            with self.subTest(name=name), self.assertRaisesRegex(ValueError,'changed'):
                additive.plan_addition(self.source,self.expected,altered)
        with self.assertRaisesRegex(ValueError,'Only one'):
            additive.plan_addition(self.source,self.expected,{**actual,'billing/migrations/0023_extra.py':'d'*64})

    def test_sql_contains_no_candidate_code_and_validates_identifiers_again(self):
        plan = additive.canonical_plan(self.path,self.expected)
        sql = additive.sql_for(plan)
        self.assertIn('ALTER TABLE public."billing_savedreport" ADD COLUMN "archived_at" timestamp with time zone NULL',sql)
        self.assertIn('SET LOCAL lock_timeout=',sql)
        self.assertIn('SET LOCAL statement_timeout=',sql)
        self.assertIn('new_access IS DISTINCT FROM old_access',sql)
        self.assertNotIn('manage.py',sql)
        for key,value in [('column','x";DROP TABLE t'),('table','auth_user'),('name',"x';DROP TABLE t")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                additive.sql_for({**plan,key:value})

    def test_installed_module_hash_matches_executor_pin(self):
        self.assertEqual(agent.ADDITIVE_MODULE_SHA256,additive.digest(DIRECTORY/'additive_migrations.py'))

    def test_helper_must_be_root_owned_before_loading(self):
        with patch.object(agent.Path,'lstat') as info:
            info.return_value.st_uid = 12345
            info.return_value.st_mode = 0o100644
            with self.assertRaisesRegex(ValueError,'root-owned'):
                agent.load_additive_module()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name).resolve()
        self.root, self.stage = folder/'root', folder/'stage'
        source = self.stage/'source'
        for base in (self.root,source):
            (base/'billing/migrations').mkdir(parents=True)
            (base/PREVIOUS).write_text('previous migration')
            (base/'billing/migrations/__init__.py').write_text('')
            (base/'compose.yaml').write_text('installed compose')
        (source/PATH).write_text(CANONICAL)
        self.config = {'runtime':'combined','compatibility':
                       {name:additive.digest(self.root/name) for name in
                        (PREVIOUS,'billing/migrations/__init__.py','compose.yaml')}}
        self.config_path = folder/'release.json'
        agent.write_json(self.config_path,self.config)
        self.plan = additive.canonical_plan(source/PATH,self.config['compatibility'])
        self.run = unittest.mock.Mock(return_value='Backup validated and uploaded at 20260922T010203Z\n')
        self.protection = patch.object(additive,'protected').start()
        self.addCleanup(patch.stopall)

    def apply(self,write=agent.write_json):
        return additive.apply(self.root,self.stage,RELEASE,self.plan,self.config,self.run,write,self.config_path)

    def test_backup_precedes_sql_and_baseline_is_extended_only_after_verification(self):
        original = copy.deepcopy(self.config)
        def verified(*args):
            self.assertEqual(json.loads(self.config_path.read_text()),original)
            self.run.assert_called_once()
            self.assertFalse((self.root/PATH).exists())
            return {'migration':NAME,'verified':True}
        with patch.object(additive,'execute_sql',side_effect=verified) as sql:
            state = self.apply()
        self.assertEqual(state['phase'],'complete')
        self.assertEqual(self.config['compatibility'],{**original['compatibility'],PATH:self.plan['sha256']})
        self.assertEqual((self.root/PATH).read_text(),CANONICAL)
        self.assertEqual(json.loads((self.stage/'additive-migration.json').read_text())['before_config'],original)
        with patch.object(additive,'execute_sql') as sql:
            self.assertEqual(self.apply()['phase'],'complete')
            sql.assert_not_called()

    def test_backup_failure_never_runs_sql(self):
        self.run.return_value = ''
        before = self.config_path.read_bytes()
        with patch.object(additive,'execute_sql') as sql, self.assertRaisesRegex(ValueError,'backup receipt'):
            self.apply()
        sql.assert_not_called()
        self.assertEqual(self.config_path.read_bytes(),before)
        self.assertFalse((self.root/PATH).exists())

    def test_database_failure_preserves_baseline_and_retries_with_existing_backup(self):
        before = self.config_path.read_bytes()
        with patch.object(additive,'execute_sql',side_effect=subprocess.TimeoutExpired(['client'],120)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.apply()
        self.assertEqual(self.config_path.read_bytes(),before)
        self.assertFalse((self.root/PATH).exists())
        self.assertEqual(json.loads((self.stage/'additive-migration.json').read_text())['phase'],'backed-up')
        with patch.object(additive,'execute_sql',return_value={'migration':NAME,'verified':True}):
            self.assertEqual(self.apply()['phase'],'complete')
        self.run.assert_called_once()

    def test_commit_then_baseline_failure_reverifies_database_on_retry(self):
        def fail_baseline(path,value):
            if Path(path) == self.config_path:
                raise OSError('simulated atomic baseline write failure')
            agent.write_json(path,value)
        with patch.object(additive,'execute_sql',return_value={'migration':NAME,'verified':True}) as sql:
            with self.assertRaises(OSError):
                self.apply(fail_baseline)
            self.assertEqual(json.loads((self.stage/'additive-migration.json').read_text())['phase'],'database-verified')
            self.assertNotIn(PATH,json.loads(self.config_path.read_text())['compatibility'])
            self.assertEqual(self.apply()['phase'],'complete')
            self.assertEqual(sql.call_count,2)
        self.run.assert_called_once()

    def test_baseline_commit_then_receipt_failure_is_recoverable(self):
        def fail_receipt(path,value):
            if Path(path).name == 'additive-migration.json' and value['phase'] == 'complete':
                raise OSError('simulated receipt write failure')
            agent.write_json(path,value)
        with patch.object(additive,'execute_sql',return_value={'migration':NAME,'verified':True}) as sql:
            with self.assertRaises(OSError):
                self.apply(fail_receipt)
            self.config = json.loads(self.config_path.read_text())
            self.assertEqual(self.apply()['phase'],'complete')
            self.assertEqual(sql.call_count,2)

    def test_interrupted_migration_file_copy_is_replaced_from_verified_source(self):
        temporary = (self.root/PATH).with_suffix('.additive-new')
        temporary.write_text('partial previous write')
        with patch.object(additive,'execute_sql',return_value={'migration':NAME,'verified':True}):
            self.assertEqual(self.apply()['phase'],'complete')
        self.assertFalse(temporary.exists())
        self.assertEqual((self.root/PATH).read_text(),CANONICAL)

    def test_rollback_keeps_committed_migration_file(self):
        with patch.object(additive,'execute_sql',return_value={'migration':NAME,'verified':True}):
            receipt = self.apply()
        (self.root/'.env').write_text('BILLING_APP_IMAGE=old\n')
        (self.stage/'source/billing/new.py').write_text('new application')
        self.config.update(root=str(self.root),releases=str(self.stage.parent),runtime='web')
        executor = agent.Agent(self.config,RELEASE,'version','b'*64)
        # Agent computes its normal release directory from release_id.
        executor.stage = self.stage
        executor.journal = self.stage/'state.json'
        executor.current.parent.mkdir(exist_ok=True)
        state = executor.state()
        state.update(image_id='sha256:'+'c'*64,additive_receipt={'phase':'complete','backup_stamp':receipt['backup_stamp']})
        executor.save(state,'staged')
        with patch.object(agent,'run',return_value=''),patch.object(agent.time,'sleep'), \
             patch.object(executor,'health',side_effect=[ValueError('unhealthy')]*20+[None]):
            with self.assertRaisesRegex(ValueError,'unhealthy'):
                executor.activate()
        self.assertEqual((self.root/PATH).read_text(),CANONICAL)
        self.assertFalse((self.root/'billing/new.py').exists())
        self.assertIn(PATH,json.loads(self.config_path.read_text())['compatibility'])


class ContainerTests(unittest.TestCase):
    def test_timeout_stops_only_its_named_client_before_propagating(self):
        identity = 'a'*64
        name = 'billing-additive-'+RELEASE
        database = json.dumps([{'State':{'Running':True},'NetworkSettings':{'Networks':{additive.NETWORK:{}}},
                                'Image':'sha256:'+'b'*64}])
        outputs = [database,'',subprocess.TimeoutExpired(['docker','run'],120),
                   identity+' '+name,'','', '']
        plan = {'version':1,'table':'billing_savedreport','column':'archived_at','name':NAME,
                'prior':['0021_activationrequest'],'dependency':'0021_activationrequest'}
        with patch.object(additive,'protected'),patch.object(additive,'sql_for',return_value='fixed SQL'):
            run = unittest.mock.Mock(side_effect=outputs)
            with self.assertRaises(subprocess.TimeoutExpired):
                additive.execute_sql(Path('/fixed/root'),RELEASE,plan,run)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn(['docker','stop','--time','10',identity],calls)
        self.assertIn(['docker','wait',identity],calls)
        self.assertFalse(any(additive.DB_CONTAINER in call and 'stop' in call for call in calls))
        container = next(call for call in calls if call[:2] == ['docker','run'])
        self.assertIn('--read-only',container)
        self.assertNotIn('/app',str(container))

    def test_unconfirmed_cleanup_is_not_reported_as_success(self):
        identity = 'a'*64
        name = 'billing-additive-'+RELEASE
        run = unittest.mock.Mock(side_effect=[identity+' '+name,'','',identity+' '+name,
                    json.dumps([{'State':{'Running':True,'Restarting':False}}])])
        with self.assertRaisesRegex(ValueError,'cleanup unconfirmed'):
            additive.stop_container(run,name)

