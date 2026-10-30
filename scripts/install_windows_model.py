"""Explicit, checksum-pinned model installation; never runs gallery processing."""
import hashlib
import json
from pathlib import Path
import urllib.request

REVISION = '36fc16bc95133424921bcc3da009e83b2f23ffb5'
FILES = {
    'LFM2.5-VL-1.6B-Q8_0.gguf': {'sha256': 'a34bd1506a298d7ff07902e69baeac48c7c20bb85162e61218b743dc10be7c67', 'size': 1246254880},
    'mmproj-LFM2.5-VL-1.6b-Q8_0.gguf': {'sha256': '2ce89e610c56f3198ece2b86cf61743a08b9307279c89125eb2412ebb908689d', 'size': 583109888}}


def install(root=Path('C:/PhotoTools')):
    folder = root / 'models/lfm2.5-vl-1.6b'
    folder.mkdir(parents=True, exist_ok=True)
    for name, evidence in FILES.items():
        target = folder / name
        if not target.exists():
            url = f'https://huggingface.co/LiquidAI/LFM2.5-VL-1.6B-GGUF/resolve/{REVISION}/{name}'
            temporary = target.with_suffix('.part')
            with urllib.request.urlopen(url, timeout=120) as response, temporary.open('wb') as output:
                while block := response.read(4 * 1024 * 1024):
                    output.write(block)
            temporary.replace(target)
        with target.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if target.stat().st_size != evidence['size'] or digest != evidence['sha256']:
            raise ValueError('Model checksum mismatch: ' + name)
        print('Verified ' + name, flush=True)
    executable = root / 'vision-runtime/b11912/llama-server.exe'
    if not executable.is_file():
        raise FileNotFoundError(executable)
    (folder / 'installation.json').write_text(json.dumps({'revision': REVISION, 'files': FILES,
                                                         'executable': str(executable)}, indent=2))


if __name__ == '__main__':
    install()
