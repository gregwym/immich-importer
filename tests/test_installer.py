import argparse
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('camera_installer', Path(__file__).resolve().parents[1] / 'tools/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTest(unittest.TestCase):
    def test_npm_installs_only_pinned_tool_in_user_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            with patch.object(installer, 'find_tool', return_value=None), patch.object(installer.shutil, 'which', return_value='/bin/npm'), patch.object(installer, 'run', side_effect=['', '3.1.0']) as run:
                result = installer.ensure_immich(prefix, {'PATH': '/bin'})
            self.assertEqual(result, str(prefix / 'bin/immich'))
            self.assertEqual(run.call_args_list[0].args[0], ['/bin/npm', 'install', '--global', '--prefix', str(prefix), '--engine-strict', '--no-audit', '--no-fund', '@immich/cli@3.1.0'])

    def test_existing_immich_does_not_call_npm(self):
        with patch.object(installer, 'find_tool', return_value='/existing/immich'), patch.object(installer, 'run', return_value='3.1.0') as run:
            self.assertEqual(installer.ensure_immich(Path('/unused'), {'PATH': '/bin'}), '/existing/immich')
            run.assert_called_once_with(['/existing/immich', '--version'], {'PATH': '/bin'})

    def test_archive_rejects_traversal_even_with_matching_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'archive.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                info = tarfile.TarInfo('exiftool-' + installer.EXIF_COMMIT + '/../escape')
                info.size = 1
                tar.addfile(info, io.BytesIO(b'x'))
            with patch.object(installer, 'EXIF_SHA256', hashlib.sha256(archive.read_bytes()).hexdigest()):
                with self.assertRaises(installer.InstallError):
                    installer.extract_verified(archive, root / 'output')
            self.assertFalse((root / 'escape').exists())

    def test_archive_hash_failure_does_not_extract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'bad.tar.gz'
            archive.write_bytes(b'bad')
            with self.assertRaisesRegex(installer.InstallError, 'SHA-256'):
                installer.extract_verified(archive, root / 'out')
            self.assertFalse((root / 'out').exists())

    def test_wrapper_preserves_arguments_and_rejects_unrelated_command(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'camera-import'
            path.write_text('unrelated command')
            with self.assertRaises(installer.InstallError):
                installer.write_wrapper(path, 'exit 0\n')
            self.assertEqual(path.read_text(), 'unrelated command')
            path.unlink()
            installer.write_wrapper(path, 'exec /bin/printf "%s\\n" "$@"\n')
            result = installer.run([str(path), 'a b', "it's quoted"])
            self.assertEqual(result, "a b\nit's quoted")

    def test_profile_is_appended_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            profile = home / '.profile'
            profile.write_text('# existing\n')
            installer.add_profile(home, home / '.local')
            once = profile.read_text()
            installer.add_profile(home, home / '.local')
            self.assertEqual(profile.read_text(), once)
            self.assertTrue(once.startswith('# existing\n'))

    def test_old_node_stops_before_installing_tools(self):
        with patch.object(installer, 'runtime', side_effect=['/node', '/perl']), patch.object(installer, 'run', return_value='18.20.0'), patch.object(installer, 'ensure_exiftool') as exif:
            with self.assertRaisesRegex(installer.InstallError, 'Node 20'):
                installer.install(argparse.Namespace(prefix=None, no_profile=True, exiftool_archive=None))
            exif.assert_not_called()

    def test_install_and_reinstall_preserve_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            sentinel = prefix / 'existing-auth.yml'
            sentinel.write_text('preserve credentials')
            args = argparse.Namespace(prefix=str(prefix), no_profile=True, exiftool_archive=None)
            with patch.object(installer, 'runtime', side_effect=lambda name, var: '/runtime/' + name), patch.object(installer, 'run', return_value='24.0.0'), patch.object(installer, 'find_tool', return_value='/existing/immich'), patch.object(installer, 'ensure_exiftool', return_value='/existing/exiftool'), patch.object(installer, 'ensure_immich', return_value='/existing/immich'):
                installer.install(args)
                first = (prefix / 'bin/camera-import').read_bytes()
                installer.install(args)
                self.assertEqual(first, (prefix / 'bin/camera-import').read_bytes())
            self.assertEqual(sentinel.read_text(), 'preserve credentials')
            self.assertEqual(len(list((prefix / 'share/immich-importer/releases').iterdir())), 2)
            self.assertTrue((prefix / 'bin/camera-repair-time').exists())
