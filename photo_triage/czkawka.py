"""Pinned read-only Czkawka runner. Never delegate deletion to the scanner."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

VERSION = '12.0.2'
SHA256 = 'eb7c2009d2dd49cf5202acbd65370caece0a3f819b9515e727cedb1ec088ed1a'
DEFAULT_EXE = Path('C:/PhotoTools/tools/czkawka/12.0.2/windows_czkawka_cli.exe')


def verify_executable(executable=DEFAULT_EXE):
    executable = Path(executable).resolve(strict=True)
    with executable.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != SHA256:
        raise ValueError('Czkawka executable differs from pinned official release')
    return executable


def command(mode, directories, output, executable=DEFAULT_EXE, difference=10):
    if mode not in {'exact', 'similar'}:
        raise ValueError('Only read-only exact and similar-image comparisons are supported')
    if type(difference) is not int or not 0 <= difference <= 40:
        raise ValueError('Image difference must be an integer from 0 to 40')
    roots = [Path(p).resolve(strict=True) for p in directories]
    if not roots or any(not p.is_dir() for p in roots):
        raise ValueError('Supply existing, explicitly scoped directories')
    args = [str(executable), 'dup' if mode == 'exact' else 'image',
            '-d', *map(str, roots), '-m', '1', '-T', '1', '-H', '-W', '-N',
            '-D', 'NONE', '-Q', '-C', str(Path(output).resolve())]
    if mode == 'similar':
        args += ['-c', '16', '-s', str(difference), '-z', 'Lanczos3']
    else:
        args += ['-s', 'HASH', '-t', 'BLAKE3', '-L']
    return args


def _groups(value):
    """Validate both image arrays and exact duplicate size/hash maps."""
    if isinstance(value, dict):
        return [group for item in value.values() for group in _groups(item)]
    if isinstance(value, list):
        if not value:
            return []
        if all(isinstance(v, dict) and isinstance(v.get('path'), str) for v in value):
            if len(value) < 2:
                raise ValueError('Unexpected singleton Czkawka group')
            return [value]
        return [group for item in value for group in _groups(item)]
    raise ValueError('Unexpected Czkawka result schema')


def scan(mode, directories, output, *, executable=DEFAULT_EXE, difference=10, timeout=300):
    executable = verify_executable(executable)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('Use a new result path; stale reports are never reused')
    roots = [Path(p).resolve(strict=True) for p in directories]
    if any(output == p or p in output.parents for p in roots):
        raise ValueError('Reports must be outside the scanned directories')
    output.parent.mkdir(parents=True, exist_ok=True)
    args = command(mode, roots, output, executable, difference)
    result = subprocess.run(args, capture_output=True, text=True, errors='replace',
                            timeout=min(300, timeout),
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                            | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0))
    if result.returncode:
        raise RuntimeError('Czkawka failed: ' + (result.stderr + result.stdout)[-2000:])
    groups = _groups(json.loads(output.read_text(encoding='utf-8')))
    for group in groups:
        for entry in group:
            path = Path(entry['path']).resolve(strict=True)
            if not any(root in path.parents for root in roots):
                raise ValueError('Czkawka returned a path outside requested roots')
    return {'engine': 'czkawka', 'version': VERSION, 'mode': mode,
            'groups': groups, 'diagnostics': (result.stdout + result.stderr)[-12000:],
            'deletion_authorized': False,
            'coverage_note': 'Groups are findings, not evidence that every format decoded.'}
