"""Managed persistent llama.cpp server for Windows and Linux."""
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
from contextlib import contextmanager

PORT = 18766
MODEL = 'LFM2.5-VL-1.6B-Q8_0.gguf'
PROJECTOR = 'mmproj-LFM2.5-VL-1.6b-Q8_0.gguf'


def healthy():
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/health', timeout=2) as r:
            if json.load(r).get('status') != 'ok':
                return False
        with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/v1/models', timeout=2) as r:
            return any(x.get('id') == 'photo-triage-lfm' for x in json.load(r).get('data', []))
    except (OSError, ValueError):
        return False


@contextmanager
def startup_lock(root):
    """Nonblocking OS lock, automatically released on worker exit."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'startup.lock').open('a+b') as stream:
        stream.seek(0)
        stream.write(b'0')
        stream.flush()
        deadline = time.monotonic() + 180
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Triage startup lock timed out')
                time.sleep(1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def ensure(root='C:/PhotoTools', executable=None):
    root = Path(root)
    if healthy():
        return
    with startup_lock(root / 'triage-runtime'):
        if healthy():
            return
        try:
            with socket.create_connection(('127.0.0.1', PORT), timeout=1):
                raise RuntimeError('Triage port occupied by another or unhealthy model')
        except (ConnectionRefusedError, TimeoutError):
            pass
        model = root / 'models/lfm2.5-vl-1.6b'
        manifest = json.loads((model / 'installation.json').read_text())
        executable = Path(executable or manifest['executable'])
        for name, expected in manifest['files'].items():
            if (model / name).stat().st_size != expected['size']:
                raise ValueError('Triage model installation is incomplete')
        import ctypes
        if os.name == 'nt':
            class Memory(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                    (n, ctypes.c_ulonglong) for n in ('total', 'available', 'page', 'freepage', 'virtual', 'freevirtual', 'extended')]
            memory = Memory()
            memory.length = ctypes.sizeof(memory)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
                raise OSError('Cannot verify available memory')
            if memory.available < 7 * 2**30:
                raise RuntimeError('Triage needs 3 GiB plus a 4 GiB free-memory reserve')
        args = [str(executable), '-m', str(model / MODEL), '--mmproj', str(model / PROJECTOR),
                '--alias', 'photo-triage-lfm', '--host', '127.0.0.1', '--port', str(PORT),
                '-ngl', '99', '-c', '4096', '-np', '1', '--cache-ram', '0', '--no-webui']
        with (root / 'triage-runtime/server.log').open('ab') as output:
            process = subprocess.Popen(args, stdout=output, stderr=output,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) |
                                       getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0))
        ready = False
        try:
            for delay in (1, 2, 4, 8, 15, 15, 15):
                time.sleep(delay)
                if process.poll() is not None:
                    break
                if healthy():
                    ready = True
                    (root / 'triage-runtime/server.json').write_text(json.dumps({'pid': process.pid, 'model': MODEL}))
                    return
            raise RuntimeError('Triage server failed startup; inspect triage-runtime/server.log')
        finally:
            if not ready and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
