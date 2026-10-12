import json
import base64
import gzip
import re
import subprocess
import io
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import installer as i
import admin as a

class EssentialTests(unittest.TestCase):
    def test_supported_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'os-release'
            for family,versions in i.SUPPORTED.items():
                for version in versions:
                    p.write_text(f'ID={family}\nVERSION_ID="{version}"\n');i.validate_host(p)
            p.write_text('ID=fedora\nVERSION_ID=42\n')
            with self.assertRaises(i.InstallError): i.validate_host(p)

    def test_paths_and_ports(self):
        for name in ('/','relative','/opt/../etc'):
            with self.assertRaises(i.InstallError): i.validate_install_dir(name)
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));s.listen()
            self.assertFalse(i.is_port_free(s.getsockname()[1],'127.0.0.1'))
        for port in (-1,0,65536):
            with self.assertRaises(i.InstallError): i.validate_port(port)

    def test_only_own_project_and_local_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'.env').write_text('POSTGRES_PASSWORD=saved\n')
            with patch.dict(os.environ,{'DOCKER_HOST':'tcp://evil','COMPOSE_PROJECT_NAME':'other','POSTGRES_PASSWORD':'wrong'}):
                env=i.compose_environment(p)
                self.assertNotIn('DOCKER_HOST',env);self.assertNotIn('COMPOSE_PROJECT_NAME',env)
                self.assertEqual(env['POSTGRES_PASSWORD'],'saved')
                with patch.object(i,'run',return_value='') as run:
                    i.compose(p,'stop')
                    self.assertIn(i.project_name(p),run.call_args.args[0])

    def test_private_services_and_official_images(self):
        cfg={'images':{key:key+'@sha256:'+'a'*64 for key in i.DEFAULT_IMAGES},'web_mode':'nginx','http_bind':'127.0.0.1','http_port':8080,'fpm_port':9080}
        i.validate_images(cfg['images'])
        services=json.loads(i.render_compose(cfg))['services']
        self.assertNotIn('ports',services['php']);self.assertNotIn('ports',services['db']);self.assertNotIn('ports',services['redis'])
        self.assertNotIn('app',services)
        cfg['web_mode']='external';services=json.loads(i.render_compose(cfg))['services']
        self.assertNotIn('nginx',services);self.assertEqual(services['php']['ports'],['127.0.0.1:9080:9000'])
        cfg['images']['php']='php:latest'
        with self.assertRaises(i.InstallError):i.validate_images(cfg['images'])

    def test_corrupt_backup_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'stack.tar').write_bytes(b'corrupt')
            (p/'manifest.json').write_text(json.dumps({'format':'stack-backup-v1','project':'ours','sha256':'wrong'}))
            with self.assertRaises(i.InstallError):a.verify(p,'ours')
            with self.assertRaises(i.InstallError):a.verify(p,'other')

    def test_major_updates_rejected_before_mutation(self):
        with patch.object(i,'image_digest') as pull:
            for service,image in (('postgres','postgres:19-alpine'),('php','php:latest'),('redis','evil/redis:8-alpine')):
                with self.assertRaises(i.InstallError): a.update(Path('/opt/unused'),{},service,image)
            pull.assert_not_called()

    def test_failed_update_restores_cold_backup_without_requiring_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            cfg={'web_mode':'nginx','images':{'php':'php@sha256:'+'a'*64}}
            with patch.object(i,'image_digest',return_value='php@sha256:'+'b'*64), patch.object(a,'backup',return_value=p/'saved'), patch.object(i,'render_compose',return_value='{}'), patch.object(i,'compose',side_effect=i.InstallError('unhealthy')), patch.object(a,'restore') as restore:
                with self.assertRaises(i.InstallError):a.update(p,cfg,'php','php:8.4-fpm-alpine')
                restore.assert_called_once_with(p,p/'saved',cfg,take_safety=False)
                self.assertEqual(cfg['images']['php'],'php@sha256:'+'a'*64)

    def test_standalone_payload_matches_repository(self):
        root=Path(__file__).resolve().parents[1]
        packed=re.search(r'payload="([A-Za-z0-9+/=]+)"',(root/'install.sh').read_text()).group(1)
        files=json.loads(gzip.decompress(base64.b64decode(packed)))
        expected={'command.sh',*[str(p.relative_to(root)) for p in (root/'src').glob('*.py')],*[str(p.relative_to(root)) for p in (root/'examples').glob('*.example')]}
        self.assertEqual(set(files),expected)
        for name,content in files.items():self.assertEqual(content,(root/name).read_text(),name)
        with tempfile.TemporaryDirectory() as tmp:
            script=Path(tmp)/'install.sh';script.write_text((root/'install.sh').read_text())
            result=subprocess.run(['bash',str(script),'--help'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('--restore',result.stdout)

    def test_domain_and_tls_validation(self):
        import web
        for value in ('evil;rm -rf /','a\nserver{}','../a','-example.com','a..test'):
            with self.assertRaises(i.InstallError):web.domain(value)
        self.assertEqual(web.domain('Example.COM'),'example.com')
        with self.assertRaises(i.InstallError):web.domain('127.0.0.1',True)
        cfg={'web_mode':'nginx','tls_mode':'letsencrypt','domain':'example.com','email':'user@example.com','http_port':8080,'https_port':443}
        with self.assertRaises(i.InstallError):web.validate(cfg)
        cfg['http_port']=80;web.validate(cfg)
        cfg['web_mode']='external'
        with self.assertRaises(i.InstallError):web.validate(cfg)

    def test_wizard_and_independent_approval(self):
        from argparse import Namespace
        args=Namespace(install_dir='/opt/test',web_mode=None)
        with patch('builtins.input',side_effect=['','1','example.com','1','8080','y']),patch.object(i,'docker_available',return_value=True),patch('sys.stdout',new=io.StringIO()):i.wizard(args)
        self.assertEqual(args.web_mode,'nginx');self.assertEqual(args.http_port,8080)
        self.assertEqual(args.domain,'example.com')
        with patch('sys.stdin.isatty',return_value=False):
            with self.assertRaises(i.InstallError):a.approval(Namespace(confirm=None),'our_project')
        a.approval(Namespace(confirm='our_project'),'our_project')

    def test_only_lets_encrypt_internal_links_are_allowed(self):
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            with tarfile.open(path/'stack.tar','w') as archive:
                item=tarfile.TarInfo('config/letsencrypt/live/example/key.pem');item.type=tarfile.SYMTYPE;item.linkname='../../../../../etc/shadow';archive.addfile(item)
            (path/'manifest.json').write_text(json.dumps({'format':'stack-backup-v1','project':'ours','sha256':a.digest(path/'stack.tar')}))
            with self.assertRaises(i.InstallError):a.verify(path,'ours')

    def test_contract(self):
        root=Path(__file__).resolve().parents[1]
        cfg=json.loads((root/'contracts/installer.v1.json').read_text())
        self.assertEqual(cfg['services'],['php','db','redis','nginx (optional)'])
        self.assertEqual(set(cfg['commands']),set(a.COMMANDS))
        self.assertFalse(cfg['application_deployment'])

if __name__=='__main__':unittest.main()
