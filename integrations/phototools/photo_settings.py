"""Portable local settings and mandatory reviewed-wanted hash policy."""
import json
import os
from pathlib import Path
import re

DEFAULT = Path(__file__).with_name('photos.local.json')

def review_default(name):
    return Path.home()/'OneDrive'/'processed_files'/name

def load(path=None):
    path = Path(path or os.environ.get('PHOTOS_CONFIG', DEFAULT)).resolve()
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if set(data['roots']) != {'icloud', 'onedrive', 'amazon'}:
        raise ValueError('Configure exactly icloud, onedrive, amazon local galleries')
    if len(data['safe_ledgers']) < 2:
        raise ValueError('Both reviewed-wanted ledgers are mandatory')
    def absolute(value):
        p = Path(value).expanduser()
        return p.resolve() if p.is_absolute() else (path.parent/p).resolve()
    data['roots'] = {k:absolute(v) for k,v in data['roots'].items()}
    for key in ('safe_ledgers', 'dependencies'):
        data[key] = [absolute(v) for v in data.get(key, [])]
    for key in ('output', 'preservation', 'deletion_ledger', 'delivery_ledger', 'clip_model', 'nsfw_model', 'marqo_model', 'exiftool', 'icloud_database', 'review_state', 'review_folder', 'restore_folder','delete_folder','nsfw_review_folder','nsfw_confirmed_folder','import_folder','hash_index'):
        if data.get(key): data[key] = absolute(data[key])
    data.setdefault('review_state',data['output']/'review/review-state.json')
    data.setdefault('hash_index',data['review_state'].parent/'hash-index.sqlite')
    data.setdefault('review_folder',review_default('FOR_REVIEW'))
    data.setdefault('nsfw_review_folder',data['review_folder'].parent/'NSFW')
    data.setdefault('nsfw_confirmed_folder',data['nsfw_review_folder']/'CONFIRMED')
    data.setdefault('restore_folder',review_default('RESTORE'))
    data.setdefault('delete_folder',data['review_folder'].parent/'DELETE')
    data.setdefault('import_folder', data['roots']['onedrive'].parent/'IMPORT')
    data.setdefault('import_live_photos','preserve')
    if data['import_live_photos'] not in {'preserve','stills_only','preserve_components'}:raise ValueError('Unknown import Live Photo policy')
    data.setdefault('triage_batch_size',500)
    if type(data['triage_batch_size']) is not int or not 1<=data['triage_batch_size']<=10000:
        raise ValueError('triage_batch_size must be an integer from 1 to 10000')
    data.setdefault('duplicate_action','recycle')
    data.setdefault('duplicate_cleanup_enabled',False)
    if type(data['duplicate_cleanup_enabled']) is not bool:raise ValueError('duplicate_cleanup_enabled must be boolean')
    data.setdefault('active_galleries',['onedrive','icloud'])
    data.setdefault('delete_galleries',list(data['active_galleries']))
    data.setdefault('sync_direction','onedrive_to_icloud')
    for key in ('active_galleries','delete_galleries'):
        selected=data[key]
        if not isinstance(selected,list) or not selected or any(k not in ('onedrive','icloud') for k in selected) or len(set(selected))!=len(selected):
            raise ValueError(key+' must be a nonempty list of distinct onedrive/icloud galleries')
    if data['sync_direction'] not in {'both','onedrive_to_icloud'}:raise ValueError('Unknown sync direction')
    # Accept old configuration files, but never re-enable the retired reverse path.
    data['sync_direction']='onedrive_to_icloud'
    if data['duplicate_action'] not in {'recycle','review'}:raise ValueError('Unknown duplicate action')
    data.setdefault('nsfw_backend','marqo')
    data.setdefault('nsfw_threshold',.5)
    from sexual_content import options as sexual_options
    data.update(sexual_options(data))
    data.setdefault('nsfw_device','cpu')
    if data['nsfw_device'] not in {'cpu','cuda','auto'}:raise ValueError('Unknown NSFW inference device')
    data.setdefault('marqo_model',data['output'].parent/'models/marqo-nsfw-384')
    if data['nsfw_backend'] not in {'marqo','nudenet'}:raise ValueError('Unknown NSFW model backend')
    import math
    if not isinstance(data['nsfw_threshold'],(int,float)) or not math.isfinite(data['nsfw_threshold']) or not 0<data['nsfw_threshold']<1:
        raise ValueError('NSFW threshold must be between zero and one')
    roots = list(data['roots'].values())
    if any(a == b or a in b.parents or b in a.parents for i,a in enumerate(roots) for b in roots[i+1:]):
        raise ValueError('Gallery roots must be distinct and nonnested')
    for key in ('output', 'preservation'):
        dest = data[key]
        if any(dest == r or r in dest.parents or dest in r.parents for r in roots):
            raise ValueError(key + ' must be separate from galleries')
        if any(p.casefold().startswith(('onedrive','icloud')) for p in dest.parts):
            raise ValueError(key + ' must be outside synchronized folders')
    return data


def review_roots(config):
    """IMPORT provenance is separate from ordinary galleries and can be restored."""
    roots={label: root for label, root in config['roots'].items() if label != 'amazon' or Path(root).is_dir()}
    folder=config.get('import_folder')
    if folder and Path(folder).is_dir():roots['import']=Path(folder).resolve()
    return roots

def nsfw_options(config):
    backend=config.get('nsfw_backend','marqo')
    model=config.get('marqo_model',Path(config['output']).parent/'models/marqo-nsfw-384') if backend=='marqo' else config['nsfw_model']
    return Path(model),backend,config.get('nsfw_threshold',.5)

def safe_paths():
    config = os.environ.get('PHOTOS_CONFIG')
    if config:
        data=load(config);paths=data['safe_ledgers'][:]
        if data['review_state'].exists():paths.append(data['review_state'])
        return paths
    return [Path(__file__).resolve().parents[2]/'outputs/nsfw-review/nsfw-exclusions.json',
            Path.home()/'OneDrive/processed_files/nsfw-exclusions.json']

def hashes(paths, field='excluded_sha256'):
    values = set()
    for path in paths:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        items = data[field]
        if not isinstance(items,list) or any(not isinstance(h,str) or not re.fullmatch('[0-9a-fA-F]{64}',h) for h in items):
            raise ValueError('Invalid hash ledger: '+str(path))
        values.update(h.lower() for h in items)
    if field!='excluded_sha256':return values
    result=values | review_hashes({'kept','restore_pending'})
    config_path=os.environ.get('PHOTOS_CONFIG')
    if config_path:
        config=load(config_path)
        ledger=config.get('deletion_ledger')
        if ledger and ledger.exists():
            explicit=json.loads(ledger.read_text(encoding='utf-8-sig')).get('folder_deleted_sha256',[])
            if any(not re.fullmatch('[0-9a-f]{64}',h) for h in explicit):raise ValueError('Invalid DELETE decision ledger')
            result-=set(explicit)
    return result

def review_hashes(statuses):
    """Permanent keep decisions apply to every configured maintained tool."""
    config=os.environ.get('PHOTOS_CONFIG')
    if not config:return set()
    from review_queue import load_state
    data=load(config);path=data['review_state']
    if not path.exists() and any(p.exists() and any(p.iterdir()) for p in (data['review_folder'],data['restore_folder'])):
        raise ValueError('Review files exist without their ledger; recover it before processing')
    result={h for h,r in load_state(path)['items'].items() if r['status'] in statuses}
    if 'kept' in statuses:
        from nsfw_inputs import wanted
        result |= wanted(data)
    return result

def decisions(safe, deleted):
    wanted = hashes(safe)
    return wanted, hashes([deleted], 'excluded_import_sha256') - wanted

def review_move_hashes(config=None):
    """Temporary review moves must not be undone by automatic reconciliation."""
    if config is None and not os.environ.get('PHOTOS_CONFIG'):return set()
    from review_queue import load_state
    data=config if config is not None else load();state=load_state(data['review_state'])
    result={h for h,r in state['items'].items()
            if r['status'] in {'reviewing','copy_pending','restore_pending','delete_pending','deleted'}}
    from nsfw_inputs import load as external_inputs
    result |= {h for h,r in external_inputs(data)['items'].items() if r['status']=='reviewing'}
    return result
