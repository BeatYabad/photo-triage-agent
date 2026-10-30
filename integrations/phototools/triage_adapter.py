"""PhotoTools boundary to the maintained BeatYabad/photo-triage-agent core."""
from pathlib import Path
import hashlib


def provider():
    from photo_triage.vision import VisionProvider
    from local_vision_server import ensure
    return VisionProvider('http://127.0.0.1:18765/v1/chat/completions',
                          model='Qwen3-VL-4B-Instruct-Q8_0',ensure=lambda:ensure({}))


_reviewer = None


def classify(image, dimensions, frames=1):
    global _reviewer
    from photo_triage.vision import junk_result
    if _reviewer is None:
        _reviewer = provider()
    return junk_result(_reviewer.inspect(image), dimensions, frames)


def fingerprint(include_model=True):
    """Invalidate cached checks when core, prompt, runtime or model changes."""
    import photo_triage
    from operational_rule_hash import rule_file_digest
    digest = hashlib.sha256()
    for path in sorted(Path(photo_triage.__file__).parent.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(rule_file_digest(path))
    model = Path('C:/PhotoTools/models/qwen3-vl-4b')
    for path in ([model/'installation.json', *sorted(model.glob('*.gguf'))] if include_model else []):
        digest.update(path.name.encode())
        digest.update(rule_file_digest(path))
    return digest.hexdigest()


def write_similarity_report(rows,path):
    import json
    from photo_triage.similarity import candidates
    features=[];gaps=[]
    for row in rows:
        if row.get('kind')!='photo' or not row.get('sha256'):continue
        checks=row.get('checks',{})
        if not {'junk','quality'} & checks.keys():continue
        found=next((v['visual_similarity'] for v in checks.values() if v.get('visual_similarity')),None)
        if found:
            features.append({k:row[k] for k in ('path','library','sha256','bytes')}|{'phash':found['phash']})
        else:gaps.append({'path':row['path'],'reason':'No single-image similarity feature; exclusions, holds and multiframe resources are not compared'})
    from itertools import islice
    pairs=list(islice(candidates(features),10001))
    report={'engine':'photo_triage_agent','pairs':pairs[:10000],'truncated':len(pairs)>10000,
            'compared_images':len(features),'gaps':gaps,'deletion_authorized':False}
    Path(path).write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report
