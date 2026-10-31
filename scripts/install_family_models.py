"""Download only pinned official OpenCV weights; no user media is uploaded."""
import argparse
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
from photo_triage.faces import MODELS,REVISION


def fetch(url):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url,timeout=90) as response:return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in {429,500,502,503,504} or attempt==2:raise
            retry=exc.headers.get('Retry-After');delay=2**(attempt+1)
            if retry:
                try:delay=max(0,float(retry))
                except ValueError:delay=max(0,(parsedate_to_datetime(retry)-datetime.now(timezone.utc)).total_seconds())
            if delay>60:raise RuntimeError('Server requests a longer retry wait; install stopped') from exc
            time.sleep(delay)
        except (urllib.error.URLError,TimeoutError):
            if attempt==2:raise
            time.sleep(2**(attempt+1))


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=True)
    for name,(group,expected) in MODELS.items():
        target=a.output/name
        if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=expected:
            data=fetch(f'https://media.githubusercontent.com/media/opencv/opencv_zoo/{REVISION}/models/{group}/{name}')
            if hashlib.sha256(data).hexdigest()!=expected:raise ValueError('Download hash mismatch: '+name)
            part=target.with_suffix('.part');part.write_bytes(data);part.replace(target)
        license_data=fetch(f'https://raw.githubusercontent.com/opencv/opencv_zoo/{REVISION}/models/{group}/LICENSE')
        (a.output/(group+'-LICENSE')).write_bytes(license_data)
    (a.output/'installation.json').write_text(json.dumps({'revision':REVISION,'models':MODELS},indent=2))
    print('Local family face models installed and SHA-256 verified')


if __name__=='__main__':main()
