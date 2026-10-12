import json
from pathlib import Path
import sys
import tempfile
import unittest
import tarfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import admin
import installer as i
from test_installer import args, IMAGES


class AdminTests(unittest.TestCase):
    def test_contract_commands_enforced(self):
        contract=json.loads((i.REPO/'contracts/installer.v1.json').read_text())
        self.assertEqual(contract['administration']['commands'],admin.COMMANDS)

    def test_malformed_settings_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'site';i.make_layout(path,i.settings(args(path),IMAGES));admin.seal(path)
            (path/'settings.json').write_text('[]')
            with self.assertRaises(i.InstallError):admin.load(path)

    def test_ambient_environment_cannot_override_managed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'site';cfg=i.settings(args(path),IMAGES);i.make_layout(path,cfg)
            with patch.dict(i.os.environ,{'APP_IMAGE':'injected:latest','POSTGRES_PASSWORD':'injected','COMPOSE_PROJECT_NAME':'unrelated','DOCKER_HOST':'tcp://example.invalid:2375'}):
                env=i.compose_environment(path)
                self.assertEqual(env['APP_IMAGE'],IMAGES['app'])
                self.assertNotEqual(env['POSTGRES_PASSWORD'],'injected')
                self.assertNotIn('DOCKER_HOST',env)
                with patch.object(i,'run',return_value='') as command:i.compose(path,'stop')
                argv=command.call_args.args[0]
                self.assertEqual(argv[argv.index('--project-name')+1],i.project_name(path))

    def test_tampered_configuration_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'site';cfg=i.settings(args(path),IMAGES);i.make_layout(path,cfg);admin.seal(path)
            self.assertEqual(admin.load(path),cfg)
            with (path/'.env').open('a') as stream: stream.write('INJECTED=1\n')
            with self.assertRaises(i.InstallError):admin.load(path)

    def test_symlinked_data_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'site';i.make_layout(path,i.settings(args(path),IMAGES));admin.seal(path)
            (path/'data/storage').rmdir();(path/'data/storage').symlink_to(tmp)
            with self.assertRaises(i.InstallError):admin.load(path)

    def test_backup_checksums_and_archive_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);(path/'database.dump').write_bytes(b'database')
            with tarfile.open(path/'files.tar','w') as archive:
                member=tarfile.TarInfo('../escape');member.size=0;archive.addfile(member)
            (path/'manifest.json').write_text(json.dumps({'version':1,'files':{f:admin.digest(path/f) for f in ('database.dump','files.tar')}}))
            with self.assertRaises(i.InstallError):admin.verify(path)
            site=path/'site';cfg=i.settings(args(site),IMAGES);i.make_layout(site,cfg);admin.seal(site)
            (site/'public-assets.json').write_text('{}')
            with tarfile.open(path/'files.tar','w') as archive:
                for name in (*admin.FILES,'integrity.json','public-assets.json','data/public','data/storage','data/custom','data/cache','data/redis'):
                    archive.add(site/name,arcname=name)
            (path/'manifest.json').write_text(json.dumps({'version':1,'project':cfg['project'],'images':IMAGES,'files':{f:admin.digest(path/f) for f in ('database.dump','files.tar')}}))
            admin.verify(path)
            (path/'database.dump').write_bytes(b'corrupt')
            with self.assertRaises(i.InstallError):admin.verify(path)

    def test_ancestor_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'link').symlink_to(root)
            with self.assertRaises(i.InstallError):i.validate_install_dir(str(root/'link/site'))

    def test_lock_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'victim').write_text('preserved')
            (root/'.site.installer.lock').symlink_to(root/'victim')
            with self.assertRaises(OSError):admin.i.operation_lock(root/'site')
            self.assertEqual((root/'victim').read_text(),'preserved')

    def test_atomic_update_preserves_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'secret';admin.atomic(path,'old');(Path(tmp)/'secret.tmp').write_text('interrupted write');admin.atomic(path,'new')
            self.assertEqual(path.read_text(),'new');self.assertEqual(path.stat().st_mode & 0o777,0o600)

if __name__=='__main__':unittest.main()
