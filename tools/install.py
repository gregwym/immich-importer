"""User-local installer. Installs tools, never language runtimes or credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

EXIF_VERSION = '13.59'
EXIF_COMMIT = '2200871d9cef988051d2a99d67df3bda6cbb30a8'
EXIF_URL = 'https://codeload.github.com/exiftool/exiftool/tar.gz/' + EXIF_COMMIT
EXIF_SHA256 = 'e1e2ad6c6fbf568afee5993ef8b2b91ab013d21698c9304e079e633ad82776f5'
IMMICH_VERSION = '3.1.0'
MARKER = '# Managed by immich-importer installer'


class InstallError(Exception):
    pass


def run(command, env=None, timeout=60):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError('Command unavailable or timed out: ' + Path(command[0]).name) from None
    if result.returncode:
        # npm / other tools can include credentials in diagnostics; do not echo.
        raise InstallError('Command failed: ' + Path(command[0]).name + ' (exit ' + str(result.returncode) + ')')
    return result.stdout.strip()


def runtime(name, variable):
    path = shutil.which(os.environ.get(variable, name))
    if not path:
        raise InstallError(name + ' runtime is missing; install it yourself or set ' + variable)
    return os.path.abspath(path)


def atomic_write(path, contents, mode=0o644):
    if path.is_symlink():
        raise InstallError('Refusing to replace a symlink: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.install-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def check_wrapper(path):
    if path.is_symlink() or (path.exists() and MARKER not in path.read_text()[:200]):
        raise InstallError('Existing command is not managed by this installer: ' + str(path))


def write_wrapper(path, text):
    check_wrapper(path)
    atomic_write(path, ('#!/bin/sh\n' + MARKER + '\nset -eu\n' + text).encode(), 0o755)


def find_tool(name, variable, prefix, env):
    explicit = os.environ.get(variable)
    candidates = [explicit] if explicit else [str(prefix / 'bin' / name), shutil.which(name, path=env['PATH'])]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.abspath(candidate)
    if explicit:
        resolved = shutil.which(explicit, path=env['PATH'])
        if resolved:
            return resolved
        raise InstallError(variable + ' does not identify an executable')
    return None


def extract_verified(archive, destination):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != EXIF_SHA256:
        raise InstallError('ExifTool download SHA-256 mismatch; nothing extracted')
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        total = 0
        for member in members:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != 'exiftool-' + EXIF_COMMIT or '..' in parts or member.name.startswith('/'):
                raise InstallError('Invalid ExifTool archive path')
            if not (member.isfile() or member.isdir()):
                raise InstallError('Unsupported link/special file in ExifTool archive')
            total += member.size
            if total > 128 * 1024 * 1024:
                raise InstallError('ExifTool archive exceeds extraction limit')
        # Manual regular-file extraction works safely on Python 3.8.15.
        for member in members:
            relative = PurePosixPath(member.name).parts[1:]
            target = destination.joinpath(*relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def ensure_exiftool(prefix, data, perl, env, offline_archive=None):
    existing = find_tool('exiftool', 'EXIFTOOL_BIN', prefix, env)
    if existing:
        version = run([existing, '-ver'], env)
        print('Reuse ExifTool ' + version)
        return existing
    target = data / ('exiftool-' + EXIF_VERSION)
    launcher = prefix / 'bin/exiftool'
    check_wrapper(launcher)
    if not target.exists():
        data.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.exiftool-install-', dir=data) as temporary:
            temporary = Path(temporary)
            archive = temporary / 'exiftool.tar.gz'
            if offline_archive:
                shutil.copyfile(offline_archive, archive)
            else:
                print('Download verified ExifTool ' + EXIF_VERSION)
                try:
                    with urllib.request.urlopen(EXIF_URL, timeout=60) as response, archive.open('wb') as output:
                        size = 0
                        while True:
                            block = response.read(1024 * 1024)
                            if not block:
                                break
                            size += len(block)
                            if size > 32 * 1024 * 1024:
                                raise InstallError('ExifTool download exceeds size limit')
                            output.write(block)
                except OSError:
                    raise InstallError('ExifTool download failed; check network/CA certificates') from None
            extracted = temporary / 'extracted'
            extracted.mkdir()
            extract_verified(archive, extracted)
            if run([perl, str(extracted / 'exiftool'), '-ver'], env) != EXIF_VERSION:
                raise InstallError('ExifTool version self-test failed')
            os.rename(extracted, target)
    if run([perl, str(target / 'exiftool'), '-ver'], env) != EXIF_VERSION:
        raise InstallError('Installed ExifTool version does not match')
    write_wrapper(launcher, 'exec ' + shlex.quote(perl) + ' ' + shlex.quote(str(target / 'exiftool')) + ' "$@"\n')
    return str(launcher)


def ensure_immich(prefix, env):
    existing = find_tool('immich', 'IMMICH_BIN', prefix, env)
    if existing:
        version = run([existing, '--version'], env)
        print('Reuse Immich CLI ' + version)
        return existing
    npm = shutil.which(os.environ.get('NPM_BIN', 'npm'), path=env['PATH'])
    if not npm:
        raise InstallError('npm is required to install Immich CLI; provide npm with your Node runtime')
    if os.path.lexists(prefix / 'bin/immich'):
        raise InstallError('Existing unusable immich command; refusing to overwrite it')
    print('Install @immich/cli@' + IMMICH_VERSION + ' with npm into ' + str(prefix))
    run([npm, 'install', '--global', '--prefix', str(prefix), '--engine-strict', '--no-audit', '--no-fund',
         '@immich/cli@' + IMMICH_VERSION], env, timeout=600)
    executable = str(prefix / 'bin/immich')
    if run([executable, '--version'], env).lstrip('v') != IMMICH_VERSION:
        raise InstallError('Immich CLI version self-test failed')
    return executable


def add_profile(home, prefix):
    profile = home / '.profile'
    marker = '# immich-importer PATH'
    if profile.is_symlink():
        print('Profile is a symlink; PATH was not edited. Command: ' + str(prefix / 'bin/camera-import'))
        return
    previous = profile.read_text() if profile.exists() else ''
    if marker in previous:
        return
    entry = '\n' + marker + '\nexport PATH=' + shlex.quote(str(prefix / 'bin')) + ':"$PATH"\n'
    # Only append; preserve existing profile content and permissions.
    with profile.open('a') as output:
        output.write(entry)
    print('Added command directory to ~/.profile (effective in new login shells)')


def install(args):
    if sys.version_info < (3, 8):
        raise InstallError('Python 3.8+ is required; runtimes are not installed automatically')
    home = Path.home()
    prefix = Path(args.prefix).expanduser().resolve() if args.prefix else home / '.local'
    if ':' in str(prefix) or '\n' in str(prefix):
        raise InstallError('Installation prefix must not contain a colon or newline')
    node, perl = runtime('node', 'NODE_BIN'), runtime('perl', 'PERL_BIN')
    node_version = run([node, '-p', 'process.versions.node'])
    try:
        if int(node_version.split('.')[0]) < 20:
            raise ValueError()
    except ValueError:
        raise InstallError('Node 20+ is required by Immich CLI 3.1.0; runtime was not changed') from None
    run([perl, '-e', 'exit 0'])
    env = os.environ.copy()
    env['PATH'] = str(prefix / 'bin') + os.pathsep + str(Path(node).parent) + os.pathsep + env.get('PATH', '')
    # Detect missing package manager before downloading/copying anything.
    if not find_tool('immich', 'IMMICH_BIN', prefix, env) and not shutil.which(os.environ.get('NPM_BIN', 'npm'), path=env['PATH']):
        raise InstallError('npm is missing; it is required to install Immich CLI')
    data = prefix / 'share/immich-importer'
    command = prefix / 'bin/camera-import'
    check_wrapper(command)
    source = Path(__file__).resolve().parents[1] / 'camera-import.py'
    run([sys.executable, '-I', '-S', str(source), '--version'], env)
    exif = ensure_exiftool(prefix, data, perl, env, args.exiftool_archive)
    immich = ensure_immich(prefix, env)
    script = source.read_bytes()
    installed = data / 'releases' / hashlib.sha256(script).hexdigest() / 'camera-import.py'
    atomic_write(installed, script)
    wrapper = 'export PATH=' + shlex.quote(str(prefix / 'bin') + ':' + str(Path(node).parent)) + ':"$PATH"\n'
    for key, value in [('EXIFTOOL_BIN', exif), ('IMMICH_BIN', immich)]:
        wrapper += 'if [ -z "${' + key + ':-}" ]; then\n  export ' + key + '=' + shlex.quote(value) + '\nfi\n'
    wrapper += 'exec ' + shlex.quote(sys.executable) + ' -B ' + shlex.quote(str(installed)) + ' "$@"\n'
    write_wrapper(command, wrapper)
    run([str(command), '--version'], env)
    atomic_write(data / 'installation.json', json.dumps({'python': sys.executable, 'node': node, 'perl': perl,
                 'immich': immich, 'exiftool': exif, 'script': str(installed)}, indent=2).encode())
    if not args.no_profile:
        add_profile(home, prefix)
    print('Installed: ' + str(command))
    print('Existing Immich login and importer configuration were preserved.')
    print('Current shell: ' + str(command) + ' --help')


def main():
    parser = argparse.ArgumentParser(description='Install camera importer and tools without pip or runtime installation')
    parser.add_argument('--prefix', help='Default: ~/.local (also the npm global prefix)')
    parser.add_argument('--no-profile', action='store_true', help='Do not add PATH to ~/.profile')
    parser.add_argument('--exiftool-archive', type=Path, help='Use a local archive with the same pinned SHA-256')
    args = parser.parse_args()
    try:
        install(args)
        return 0
    except (InstallError, OSError) as error:
        print('INSTALL FAILED: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
