import argparse
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import installer as i

DIGEST = 'ghcr.io/cristake007/fluidweb@sha256:' + 'a' * 64
IMAGES = {'app': DIGEST, 'postgres': 'postgres@sha256:'+'b'*64,
          'redis': 'redis@sha256:'+'c'*64, 'nginx': 'nginx@sha256:'+'d'*64}


def args(path, mode='nginx'):
    return argparse.Namespace(install_dir=str(path), web_mode=mode,
                              http_port=8080, fpm_port=9080,
                              app_image=DIGEST, install_docker=False)


class Tests(unittest.TestCase):
    def test_supported_os(self):
        with tempfile.TemporaryDirectory() as tmp:
            osr=Path(tmp)/'os-release'
            for name, version in [('ubuntu','22.04'),('ubuntu','24.04'),('debian','12'),('debian','13')]:
                osr.write_text(f'ID={name}\nVERSION_ID="{version}"\n')
                i.validate_host(osr)
            for name, version in [('ubuntu','20.04'),('debian','11'),('arch','rolling')]:
                osr.write_text(f'ID={name}\nVERSION_ID={version}\n')
                with self.assertRaises(i.InstallError): i.validate_host(osr)

    def test_bootstrap_rejects_unrelated_runtime_before_changes(self):
        with patch.object(i.shutil,'which',side_effect=lambda name: '/usr/bin/containerd' if name=='containerd' else None),\
             patch.object(i,'run',side_effect=AssertionError('No host mutations permitted')):
            with self.assertRaises(i.InstallError):i.install_docker('debian','13')

    def test_docker_key_rejects_extra_or_unexpected_primary_keys(self):
        valid='pub:-:4096:1:docker\nfpr:::::::::9DC858229FC7DD38854AE2D88D81803C0EBFCD88:\n'
        i.validate_docker_key(valid)
        for text in (valid+'pub:-:4096:1:attacker\n',valid.replace('9DC858','AAAAAA'),''):
            with self.assertRaises(i.InstallError):i.validate_docker_key(text)

    def test_directory_restrictions(self):
        for path in ('relative', '/', '/opt/../etc'):
            with self.assertRaises(i.InstallError): i.validate_install_dir(path)
        self.assertEqual(i.validate_install_dir('/opt/fluidweb'), Path('/opt/fluidweb'))
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp,'pointer').symlink_to(Path(tmp,'target'))
            with self.assertRaises(i.InstallError): i.validate_install_dir(tmp+'/pointer')

    def test_port_validation(self):
        for port in (0, -1, 22, 1023, 65536):
            with self.assertRaises(i.InstallError): i.validate_port(port)
        for port in (80, 443, 1024, 8080, 65535): i.validate_port(port)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.assertFalse(i.is_port_free(sock.getsockname()[1]))

    def test_immutable_image_required(self):
        for value in ('ghcr.io/cristake007/fluidweb:dev', 'fluidweb:latest', 'fluidweb:1.0', 'fluidweb@sha256:123'):
            with self.assertRaises(i.InstallError): i.validate_app_image(value)
        i.validate_app_image(DIGEST)

    def test_layout_isolation_and_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'fluidweb'
            cfg = i.settings(args(path), IMAGES)
            i.make_layout(path, cfg)
            self.assertEqual((path/'.env').stat().st_mode & 0o777, 0o600)
            self.assertEqual((path/'data').stat().st_mode & 0o777, 0o755)
            self.assertEqual((path/'data/public').stat().st_mode & 0o777, 0o755)
            self.assertEqual((path/'data/postgres').stat().st_mode & 0o777, 0o700)
            self.assertTrue((path/'data/redis').is_dir())
            self.assertTrue((path/'data/storage').is_dir())
            self.assertTrue((path/'config/apache.conf.example').exists())
            self.assertEqual(json.loads((path/'settings.json').read_text()), cfg)
            with self.assertRaises(FileExistsError): i.make_layout(path, cfg)
            text=(path/'compose.yaml').read_text()
            self.assertNotIn('docker.sock',text)
            self.assertNotIn('ports:\n      - "5432',text)
            self.assertNotIn('ports:\n      - "6379',text)
            self.assertNotIn('down -v',text)
            self.assertIn('internal: true',text)
            self.assertIn('nginx:\n',text)

    def test_external_mode_has_no_container_nginx_and_loopback_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=i.settings(args(Path(tmp)/'a', 'external'), IMAGES)
            text=i.render_compose(cfg)
            self.assertNotIn('  nginx:\n', text)
            self.assertIn('127.0.0.1:${FPM_PORT}:9000', text)
            self.assertNotIn('0.0.0.0:9000', text)

    def test_rerun_preserves_files_and_verifies_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'site'
            a=args(path)
            cfg=i.settings(a, IMAGES)
            i.make_layout(path, cfg)
            i.safe_json(path/'installed.json', {'state':'ready'})
            import admin
            admin.seal(path)
            before={p.relative_to(path): (p.stat().st_mtime_ns,p.read_bytes())
                    for p in path.rglob('*') if p.is_file()}
            with patch.object(i,'validate_host'), patch.object(i,'check_disk'), patch.object(i.os,'geteuid',return_value=0),\
                 patch.object(i,'docker_available',return_value=True),\
                 patch.object(i,'compose',return_value='running') as lifecycle,\
                 patch.object(i,'validate_runtime') as health:
                i.install(a)
            after={p.relative_to(path): (p.stat().st_mtime_ns,p.read_bytes())
                   for p in path.rglob('*') if p.is_file()}
            self.assertEqual(before,after)
            lifecycle.assert_called_once_with(path, 'up', '-d', '--wait')
            health.assert_called_once_with(path,cfg)

    def test_failed_runtime_preserves_unchanged_state_for_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'site'; a=args(path)
            with patch.object(i,'validate_host'),patch.object(i,'check_disk'),patch.object(i.os,'geteuid',return_value=0),\
                 patch.object(i,'docker_available',return_value=True),patch.object(i,'is_port_free',return_value=True),\
                 patch.object(i,'image_digest',side_effect=lambda name: IMAGES['app'] if name == DIGEST else IMAGES[next(k for k,v in i.DEFAULT_IMAGES.items() if v==name)]),\
                 patch.object(i,'compose',side_effect=i.InstallError('simulated failed startup')),\
                 patch('admin.initialize_public'):
                with self.assertRaises(i.InstallError): i.install(a)
            self.assertTrue((path/'.env').exists())
            self.assertFalse((path/'installed.json').exists())
            secrets_before=(path/'.env').read_bytes()
            with patch.object(i,'validate_host'),patch.object(i,'check_disk'),patch.object(i.os,'geteuid',return_value=0),\
                 patch.object(i,'docker_available',return_value=True),patch.object(i,'compose',return_value='running'),\
                 patch.object(i,'validate_runtime',return_value=None),patch('admin.initialize_public'):
                i.install(a)
            self.assertEqual(secrets_before,(path/'.env').read_bytes())
            self.assertEqual(json.loads((path/'installed.json').read_text())['state'],'ready')

    def test_unknown_state_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'site';path.mkdir(); (path/'other-app.conf').write_text('critical')
            with patch.object(i,'validate_host'),patch.object(i,'check_disk'),patch.object(i.os,'geteuid',return_value=0),\
                 patch.object(i,'run',side_effect=AssertionError('Must not call docker')):
                with self.assertRaises(i.InstallError):i.install(args(path))
            self.assertEqual((path/'other-app.conf').read_text(),'critical')

    def test_contract_values_match_implementation(self):
        contract=json.loads((i.REPO/'contracts/installer.v1.json').read_text())
        self.assertEqual(contract['supported_os'], {k: sorted(v) for k,v in i.SUPPORTED.items()})
        self.assertEqual(contract['installer']['default_path'],'/opt/fluidweb')
        self.assertEqual(contract['installer']['web_modes'],['nginx','external'])
        self.assertFalse(contract['production_ready'])


if __name__ == '__main__': unittest.main()
