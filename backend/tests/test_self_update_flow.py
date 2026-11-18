"""Real-Git update transactions; Docker/HTTP effects are controlled test doubles.

These tests never touch the selected checkout, live containers or business DB.
A canonical GitHub URL is rewritten only inside a temporary Git configuration
into a temporary bare repository, so remote identity checks remain exercised.
"""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import unittest


PROJECT = Path(__file__).resolve().parents[2]
CANONICAL = 'https://github.com/JiangYu3761/amazon-ops-agent.git'


@unittest.skipUnless(shutil.which('git') and shutil.which('sh') and (PROJECT/'scripts'/'self_update.sh').is_file(), 'requires the source checkout, Git and a POSIX shell')
class SelfUpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix='homa-update-transaction-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'source'
        self.deploy = self.root / 'deployment'
        self.remote = self.root / 'remote.git'
        self.bin = self.root / 'bin'
        self.runtime = self.root / 'runtime'
        self.bin.mkdir()
        self.runtime.mkdir()
        self.log = self.runtime / 'effects.jsonl'
        self.state = self.runtime / 'state.json'
        self.request = self.runtime / 'request.json'
        self.deployed = self.runtime / 'deployed.json'
        self.fingerprints = self.runtime / 'fingerprints.json'
        config = self.root / 'gitconfig'
        config.write_text('[url "' + self.remote.as_uri() + '"]\n'
                          '    insteadOf = ' + CANONICAL + '\n')
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=str(config),
                        GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
                        APP_DIR=str(self.deploy), APP_BRANCH='main',
                        UPDATE_STATE_FILE=str(self.state),
                        UPDATE_REQUEST_FILE=str(self.request),
                        UPDATE_HEARTBEAT_FILE=str(self.runtime / 'heartbeat.json'),
                        UPDATE_LOCK_FILE=str(self.runtime / 'worker.lock'),
                        BACKUP_DIR=str(self.runtime / 'backups'),
                        APP_HEALTH_URL='http://fixture.invalid/health',
                        UPDATE_HEALTH_ATTEMPTS='2', UPDATE_HEALTH_INTERVAL='0',
                        UPDATE_TEST_LOG=str(self.log), UPDATE_TEST_FAIL='',
                        UPDATE_TEST_DEPLOYED=str(self.deployed),
                        UPDATE_TEST_FINGERPRINTS=str(self.fingerprints),
                        PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''))
        self._command(['git', 'init', '--bare', str(self.remote)])
        self._command(['git', 'init', '-b', 'main', str(self.source)])
        self._git(self.source, 'config', 'user.name', 'Update transaction tests')
        self._git(self.source, 'config', 'user.email', 'update-tests@example.invalid')
        (self.source / 'scripts').mkdir()
        (self.source / 'db').mkdir()
        (self.source / 'backend' / 'app').mkdir(parents=True)
        (self.source / 'frontend').mkdir()
        (self.source / 'backend' / 'app' / 'version.py').write_text('VERSION = "one"\n')
        (self.source / 'frontend' / 'catalog.json').write_text('{}\n')
        (self.source / 'version.txt').write_text('one\n')
        (self.source / 'docker-compose.yml').write_text('services: {}\n')
        (self.source / 'db' / '001_fixture.sql').write_text('SELECT 1;\n')
        (self.source / 'scripts' / 'migrate.sh').write_bytes((PROJECT / 'scripts' / 'migrate.sh').read_bytes())
        self._git(self.source, 'add', '.')
        self._git(self.source, 'commit', '-m', 'initial deployment')
        self.original = self._git(self.source, 'rev-parse', 'HEAD').strip()
        initial_fingerprint=self.fingerprint(self.source)
        self.fingerprints.write_text(json.dumps({self.original:initial_fingerprint}))
        self.deployed.write_text(json.dumps({'commit':self.original,'source_fingerprint':initial_fingerprint}))
        self.env['UPDATE_TEST_ORIGINAL_COMMIT']=self.original
        self._git(self.source, 'remote', 'add', 'origin', CANONICAL)
        self._git(self.source, 'push', 'origin', 'main')
        self._command(['git', 'clone', '--branch', 'main', CANONICAL, str(self.deploy)])
        self._git(self.deploy, 'config', 'user.name', 'Update transaction tests')
        self._git(self.deploy, 'config', 'user.email', 'update-tests@example.invalid')
        self._double('docker', self._docker_double())
        self._double('curl', self._curl_double())

    def _command(self, args, cwd=None, check=True, env=None, timeout=25):
        result = subprocess.run(args, cwd=cwd, env=env or self.env, text=True,
                                capture_output=True, timeout=timeout)
        if check and result.returncode:
            self.fail('Command failed: ' + repr(args) + '\n' + result.stdout + result.stderr)
        return result

    def _git(self, path, *args):
        return self._command(['git', '-C', str(path), *args]).stdout

    def _double(self, name, body):
        script = self.bin / name
        script.write_text('#!' + (shutil.which('python3') or '/usr/bin/python3') + '\n' + body)
        script.chmod(0o700)

    @staticmethod
    def fingerprint(root):
        app=root/'backend'/'app'
        digest=hashlib.sha256()
        for path in sorted(app.rglob('*.py'),key=lambda p:p.relative_to(app).as_posix()):
            digest.update(('app/'+path.relative_to(app).as_posix()).encode()+b'\0'+path.read_bytes()+b'\0')
        digest.update(b'catalog.json\0'+(root/'frontend'/'catalog.json').read_bytes()+b'\0')
        return digest.hexdigest()

    @staticmethod
    def _docker_double():
        return '''import io,json,os,sys,tarfile
from pathlib import Path
args=sys.argv[1:]
version=Path(os.environ['APP_DIR'],'version.txt').read_text().strip()
with open(os.environ['UPDATE_TEST_LOG'],'a') as log:
 log.write(json.dumps({'tool':'docker','args':args,'version':version})+'\\n')
mode=os.environ.get('UPDATE_TEST_FAIL','')
if args and args[0]=='info':sys.exit(0)
if args and args[0]=='inspect':
 cid=args[-1]
 service='web' if 'web' in cid else 'api'
 print('sha256:old-'+service+'|fixture-'+service+':previous')
 sys.exit(0)
if args and args[0]=='tag':sys.exit(0)
if 'ps' in args and '-q' in args:
 print('fixture-'+args[-1]+'-container')
 sys.exit(0)
if 'pg_dump' in args:
 if mode=='backup':
  print('-- private partial fixture dump')
  sys.exit(31)
 print('-- isolated test dump\\nSELECT 1;')
 sys.exit(0)
if 'python' in args or 'python3' in args:
 data=io.BytesIO()
 with tarfile.open(fileobj=data,mode='w:gz') as archive:
  content=b'fixture retained attachment'
  entry=tarfile.TarInfo('workflow-uploads/fixture.txt');entry.size=len(content)
  archive.addfile(entry,io.BytesIO(content))
 sys.stdout.buffer.write(data.getvalue())
 sys.exit(0)
if 'build' in args and mode=='interrupt' and version=='two':
 import signal
 os.kill(os.getppid(),signal.SIGKILL)
 sys.exit(35)
if 'build' in args and mode=='build' and version=='two':sys.exit(32)
if 'psql' in args:
 sys.stdin.buffer.read()
 if mode=='migration' and version=='two':sys.exit(33)
 sys.exit(0)
if 'up' in args and mode=='restart' and version=='two':sys.exit(34)
if 'up' in args:
 commit=os.environ.get('APP_COMMIT')
 hashes=json.loads(Path(os.environ['UPDATE_TEST_FINGERPRINTS']).read_text())
 fingerprint=hashes.get(commit)
 if mode=='fingerprint' and commit!=os.environ['UPDATE_TEST_ORIGINAL_COMMIT']:fingerprint=hashes[os.environ['UPDATE_TEST_ORIGINAL_COMMIT']]
 Path(os.environ['UPDATE_TEST_DEPLOYED']).write_text(json.dumps({'commit':commit,'source_fingerprint':fingerprint}))
sys.exit(0)
'''

    @staticmethod
    def _curl_double():
        return '''import json,os,sys
from pathlib import Path
version=Path(os.environ['APP_DIR'],'version.txt').read_text().strip()
with open(os.environ['UPDATE_TEST_LOG'],'a') as log:
 log.write(json.dumps({'tool':'curl','args':sys.argv[1:],'version':version})+'\\n')
deployed=json.loads(Path(os.environ['UPDATE_TEST_DEPLOYED']).read_text())
if os.environ.get('UPDATE_TEST_FAIL')=='health' and deployed.get('commit')!=os.environ['UPDATE_TEST_ORIGINAL_COMMIT']:sys.exit(22)
if any('/api/workspace' in a for a in sys.argv):print('{"products":[],"tasks":[]}')
elif any('/api/system/version' in a for a in sys.argv):
 counter=Path(os.environ['UPDATE_TEST_DEPLOYED']).parent/'version-probes'
 count=int(counter.read_text())+1 if counter.exists() else 1
 counter.write_text(str(count))
 threshold=int(os.environ.get('UPDATE_TEST_VERSION_FAIL_AFTER','0'))
 if threshold and count>threshold:sys.exit(22)
 print(json.dumps(deployed))
else:print('{"status":"ok"}')
'''

    def publish(self):
        (self.source / 'version.txt').write_text('two\n')
        (self.source / 'backend' / 'app' / 'version.py').write_text('VERSION = "two"\n')
        (self.source / 'db' / '002_fixture.sql').write_text('SELECT 2;\n')
        self._git(self.source, 'add', '.')
        self._git(self.source, 'commit', '-m', 'verified update')
        self._git(self.source, 'push', 'origin', 'main')
        target=self._git(self.source, 'rev-parse', 'HEAD').strip()
        hashes=json.loads(self.fingerprints.read_text())
        hashes[target]=self.fingerprint(self.source)
        self.fingerprints.write_text(json.dumps(hashes))
        return target

    def update(self, failure=''):
        env = {**self.env, 'UPDATE_TEST_FAIL': failure}
        return self._command(['sh', str(PROJECT / 'scripts' / 'self_update.sh')],
                             cwd=self.deploy, check=False, env=env)

    def effects(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def status(self):
        self.assertTrue(self.state.is_file(), 'worker must persist its outcome')
        return json.loads(self.state.read_text())

    def assert_original(self):
        self.assertEqual(self._git(self.deploy, 'rev-parse', 'HEAD').strip(), self.original)
        self.assertEqual((self.deploy / 'version.txt').read_text(), 'one\n')

    def test_no_update_keeps_checkout_and_does_not_back_up_or_build(self):
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + repr(self.status()))
        self.assertEqual(self.status()['status'], 'up_to_date')
        self.assert_original()
        effects = self.effects()
        self.assertFalse(any('pg_dump' in e['args'] or 'build' in e['args'] for e in effects))

    def test_clean_fast_forward_updates_and_runs_backups_migration_and_health(self):
        target = self.publish()
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + repr(self.status()))
        self.assertEqual(self.status()['status'], 'succeeded')
        self.assertEqual(self._git(self.deploy, 'rev-parse', 'HEAD').strip(), target)
        effects = self.effects()
        self.assertTrue(any('pg_dump' in e['args'] for e in effects))
        self.assertTrue(any('psql' in e['args'] and e['version']=='two' for e in effects))
        self.assertTrue(any('build' in e['args'] and e['version']=='two' for e in effects))
        self.assertTrue(any(e['tool']=='curl' and e['version']=='two' for e in effects))
        dumps = list((self.runtime/'backups').rglob('*.sql.gz'))
        self.assertTrue(dumps, 'a successful update must retain a database backup')
        self.assertIn(b'isolated test dump', gzip.decompress(dumps[0].read_bytes()))
        self.assertEqual(dumps[0].stat().st_mode & 0o777,0o600)
        files=list((self.runtime/'backups').rglob('*-files.tar.gz'))
        self.assertTrue(files,'uploaded files must also be backed up')
        with tarfile.open(files[0],'r:gz') as archive:
            self.assertEqual(archive.extractfile('workflow-uploads/fixture.txt').read(),b'fixture retained attachment')

    def test_database_backup_failure_stops_before_checkout_switch(self):
        self.publish()
        result = self.update('backup')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.status()['status'], 'failed')
        self.assert_original()
        self.assertFalse(any('build' in e['args'] for e in self.effects()))
        for artifact in (self.runtime/'backups').iterdir():
            if artifact.is_file():
                self.assertEqual(artifact.stat().st_mode & 0o077,0,'even failed or partial backups must remain private')

    def test_build_and_migration_failures_restore_original_code(self):
        for mode in ('build','migration'):
            with self.subTest(failure=mode):
                # Both attempts start on the original revision and fetch the same update.
                if mode=='build':self.publish()
                self.log.unlink(missing_ok=True)
                result = self.update(mode)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.status()['status'], 'failed')
                self.assert_original()
                effects=self.effects()
                self.assertTrue(any('up' in e['args'] and e['version']=='one' for e in effects))

    def test_health_failure_restores_previous_code_and_service(self):
        self.publish()
        result = self.update('health')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.status()['status'], 'failed')
        self.assert_original()
        self.assertTrue(any('up' in e['args'] and e['version']=='one' for e in self.effects()))
        self.assertTrue(any(e['tool']=='curl' and e['version']=='one' for e in self.effects()))

    def test_dirty_checkout_is_preserved_and_not_updated(self):
        self.publish()
        (self.deploy/'version.txt').write_text('uncommitted user work\n')
        result=self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.status()['status'], 'failed')
        self.assertEqual(self._git(self.deploy,'rev-parse','HEAD').strip(),self.original)
        self.assertEqual((self.deploy/'version.txt').read_text(),'uncommitted user work\n')
        self.assertFalse(any('pg_dump' in e['args'] for e in self.effects()))

    def test_diverged_checkout_is_preserved_and_not_updated(self):
        self.publish()
        (self.deploy/'local-only.txt').write_text('local commit\n')
        self._git(self.deploy,'add','.')
        self._git(self.deploy,'commit','-m','local branch work')
        local=self._git(self.deploy,'rev-parse','HEAD').strip()
        result=self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.status()['status'],'failed')
        self.assertEqual(self._git(self.deploy,'rev-parse','HEAD').strip(),local)
        self.assertEqual((self.deploy/'local-only.txt').read_text(),'local commit\n')
        self.assertFalse(any('pg_dump' in e['args'] for e in self.effects()))

    def test_unexpected_remote_is_rejected_without_network_or_checkout_changes(self):
        self._git(self.deploy,'remote','set-url','origin','https://github.com/other/project.git')
        result=self.update()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.status()['status'],'failed')
        self.assert_original()
        self.assertFalse(any('pg_dump' in e['args'] for e in self.effects()))

    def worker_once(self, failure=''):
        return self._command(['sh', str(PROJECT/'scripts'/'update_worker.sh'), '--once'],
                             cwd=self.deploy, check=False,
                             env={**self.env,'UPDATE_TEST_FAIL':failure})

    def test_interrupted_worker_recovers_and_duplicate_request_has_no_repeat_effects(self):
        target=self.publish()
        self.request.write_text(json.dumps({'request_id':'interrupted-fixture-update'}))
        interrupted=self.worker_once('interrupt')
        self.assertNotEqual(interrupted.returncode,0)
        active=self.runtime/'update.active.json'
        self.assertTrue(active.is_file(),'an interrupted claimed request must remain recoverable')
        self.assertEqual(self._git(self.deploy,'rev-parse','HEAD').strip(),target)
        recovered=self.worker_once()
        self.assertEqual(recovered.returncode,0,recovered.stdout+recovered.stderr)
        self.assert_original()
        self.assertFalse(active.exists())
        self.assertEqual(self.status()['status'],'failed')
        self.assertEqual(self.status()['recovery_status'],'restored')
        self.request.write_text(json.dumps({'request_id':'retry-after-recovery'}))
        retried=self.worker_once()
        self.assertEqual(retried.returncode,0,retried.stdout+retried.stderr)
        self.assertEqual(self.status()['status'],'succeeded')
        self.assertEqual(self._git(self.deploy,'rev-parse','HEAD').strip(),target)
        effect_count=sum('pg_dump' in e['args'] or 'build' in e['args'] for e in self.effects())
        self.request.write_text(json.dumps({'request_id':'retry-after-recovery'}))
        duplicate=self.worker_once()
        self.assertEqual(duplicate.returncode,0,duplicate.stdout+duplicate.stderr)
        self.assertEqual(sum('pg_dump' in e['args'] or 'build' in e['args'] for e in self.effects()),effect_count)
        self.assertFalse(active.exists())
        self.assertFalse(self.request.exists())

    def test_successful_terminal_state_survives_worker_cleanup_interruption(self):
        target=self.publish()
        request={'request_id':'finished-before-cleanup'}
        self.request.write_text(json.dumps(request))
        completed=self.worker_once()
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
        self.assertEqual(self.status()['status'],'succeeded')
        # Simulate a crash after a durable success result but before queue cleanup.
        active=self.runtime/'update.active.json'
        active.write_text(json.dumps(request))
        self.log.unlink(missing_ok=True)
        resumed=self.worker_once()
        self.assertEqual(resumed.returncode,0,resumed.stdout+resumed.stderr)
        self.assertEqual(self._git(self.deploy,'rev-parse','HEAD').strip(),target)
        self.assertEqual(self.status()['status'],'succeeded')
        self.assertFalse(active.exists())
        self.assertFalse(any(e['tool']=='docker' and ('up' in e['args'] or 'tag' in e['args']) for e in self.effects()),'successful jobs must not be rolled back merely because cleanup was interrupted')

    def test_latest_checkout_with_old_running_api_is_rebuilt_and_deployed(self):
        target=self.publish()
        self._git(self.deploy,'fetch','origin','main')
        self._git(self.deploy,'reset','--hard',target)
        self.assertEqual(json.loads(self.deployed.read_text())['commit'],self.original)
        result=self.update()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr+repr(self.status()))
        self.assertEqual(self.status()['status'],'succeeded','a latest checkout alone does not establish that the running API is up to date')
        self.assertEqual(json.loads(self.deployed.read_text())['commit'],target)
        self.assertTrue(any('build' in e['args'] for e in self.effects()))
        self.assertTrue(any('up' in e['args'] for e in self.effects()))

    def test_same_checkout_redeployment_failure_restores_actual_old_runtime_identity(self):
        target=self.publish()
        self._git(self.deploy,'fetch','origin','main')
        self._git(self.deploy,'reset','--hard',target)
        result=self.update('health')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.status()['status'],'failed')
        self.assertEqual(self.status()['recovery_status'],'restored')
        self.assertEqual(self._git(self.deploy,'rev-parse','HEAD').strip(),target)
        runtime=json.loads(self.deployed.read_text())
        self.assertEqual(runtime['commit'],self.original,'restored old images must not be relabeled as the latest source commit')
        self.assertEqual(runtime['source_fingerprint'],json.loads(self.fingerprints.read_text())[self.original])
        self.assertTrue(any('tag' in e['args'] for e in self.effects()))

    def test_interrupted_same_checkout_redeployment_restores_previous_images(self):
        target=self.publish()
        self._git(self.deploy,'fetch','origin','main')
        self._git(self.deploy,'reset','--hard',target)
        self.request.write_text(json.dumps({'request_id':'interrupted-same-source-redeploy'}))
        interrupted=self.worker_once('interrupt')
        self.assertNotEqual(interrupted.returncode,0)
        self.assertTrue((self.runtime/'update.active.json').exists())
        self.assertEqual(self._git(self.deploy,'rev-parse','HEAD').strip(),target)
        self.log.unlink(missing_ok=True)
        recovered=self.worker_once()
        self.assertEqual(recovered.returncode,0,recovered.stdout+recovered.stderr)
        self.assertEqual(self.status()['status'],'failed')
        self.assertEqual(self.status()['recovery_status'],'restored','unchanged Git HEAD does not prove that image tags and runtime services were untouched')
        self.assertEqual(json.loads(self.deployed.read_text())['commit'],self.original)
        self.assertTrue(any('tag' in e['args'] for e in self.effects()))
        self.assertTrue(any('up' in e['args'] for e in self.effects()))

    def test_previous_probe_success_is_not_reused_after_version_endpoint_fails(self):
        self.env['UPDATE_TEST_VERSION_FAIL_AFTER']='1'
        result=self.update()
        self.assertNotEqual(result.returncode,0,'a persistent version-probe failure must not be reported as a verified deployment')
        self.assertEqual(self.status()['status'],'failed','a stale readiness probe cannot establish up_to_date or succeeded after the execution probe fails')
        self.assertTrue(any('build' in e['args'] for e in self.effects()))

    def test_target_commit_label_with_old_fingerprint_does_not_short_circuit_update(self):
        target=self.publish()
        self._git(self.deploy,'fetch','origin','main')
        self._git(self.deploy,'reset','--hard',target)
        old_fingerprint=json.loads(self.fingerprints.read_text())[self.original]
        self.deployed.write_text(json.dumps({'commit':target,'source_fingerprint':old_fingerprint}))
        result=self.update()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr+repr(self.status()))
        self.assertEqual(self.status()['status'],'succeeded','a claimed target commit cannot override an explicitly conflicting runtime fingerprint')
        self.assertTrue(any('build' in e['args'] for e in self.effects()))
        self.assertEqual(json.loads(self.deployed.read_text())['source_fingerprint'],json.loads(self.fingerprints.read_text())[target])

    def test_old_fingerprint_after_restart_rejects_success_and_restores_previous_images(self):
        self.publish()
        result=self.update('fingerprint')
        self.assertNotEqual(result.returncode,0,'healthy endpoints and a target commit label cannot prove that the old code was replaced')
        self.assertEqual(self.status()['status'],'failed')
        self.assertEqual(self.status()['recovery_status'],'restored')
        self.assert_original()
        self.assertEqual(json.loads(self.deployed.read_text())['source_fingerprint'],json.loads(self.fingerprints.read_text())[self.original])
        self.assertTrue(any('tag' in e['args'] for e in self.effects()))
