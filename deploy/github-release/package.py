#!/usr/bin/env python3
"""Package tracked source and native collector wheels alongside the scanned Docker artifact."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def package(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    files = subprocess.check_output(['git', 'ls-files', '-z'], text=True).split('\0')
    with tarfile.open(output / 'source.tar.gz', 'w:gz') as archive:
        for name in filter(None, files):
            path = Path(name)
            if path.is_symlink() or not path.is_file():
                raise ValueError('Source must contain only regular tracked files')
            archive.add(path, arcname=name, recursive=False)
    with tempfile.TemporaryDirectory() as temporary:
        subprocess.run([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:',
                        '--dest', temporary, '-r', 'requirements.txt'], check=True)
        with tarfile.open(output / 'wheels.tar.gz', 'w:gz') as archive:
            for path in sorted(Path(temporary).iterdir()):
                archive.add(path, arcname=path.name, recursive=False)
        with tempfile.TemporaryDirectory() as validation:
            subprocess.run([sys.executable, '-m', 'venv', validation], check=True)
            python = str(Path(validation) / 'bin/python')
            subprocess.run([python, '-m', 'pip', 'install', '--no-index', '--no-cache-dir',
                            '--find-links', temporary, '-r', 'requirements.txt'], check=True)
            subprocess.run([python, '-m', 'pip', 'check'], check=True)
    names = ['source.tar.gz', 'wheels.tar.gz', 'images.json', 'images.tar.gz']
    checksums = {}
    for name in names:
        digest = hashlib.sha256()
        with (output / name).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        checksums[name] = digest.hexdigest()
    (output / 'checksums.json').write_text(json.dumps(checksums, indent=2) + '\n')


if __name__ == '__main__':
    package(sys.argv[1])
