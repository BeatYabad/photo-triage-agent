"""Feed privacy decision folders into the existing verified decision handlers."""
import json
import subprocess
from pathlib import Path
import photo_names
import privacy_storage
import review_queue as queue
import merge_photos as media


def normalize(config):
    import restore_family
    family=restore_family.process(config)
    private = config['nsfw_review_folder']
    import sfw_feedback
    safe=sfw_feedback.process(config)
    result = {'moved': [], 'held': list(safe['held'])+family['held'], 'sfw':safe,'family':family}
    for source, destination in ((private / 'DELETE', config.get('delete_folder', config['restore_folder'].parent / 'DELETE')),):
        if not source.exists():
            continue
        for path in sorted(source.rglob('*')):
            if not path.is_file():
                continue
            try:
                media.inside(path, source)
                actual = queue.fresh(path)
                digest, size = actual['sha256'], actual['bytes']
                target = photo_names.choose(destination, path.name, digest, size)
                privacy_storage.copy_verified(path, target, destination, digest, size)
                if queue.fresh(path) != actual:
                    raise ValueError('Decision input changed during transfer')
                receipt = {'source': str(path), 'destination': str(target),
                           'backup': None, 'sha256': digest, 'bytes': size}
                log = config['output'] / 'decision-inbox-transfers.jsonl'
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(receipt) + '\n')
                path.unlink()
                result['moved'].append(receipt)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                result['held'].append({'path': str(path), 'error': str(exc)})
    return result


def finish(config):
    import nsfw_review
    return nsfw_review.service({**config, 'defer_review_deletions': False})

