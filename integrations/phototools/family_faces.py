"""User-enrolled family faces, stored locally outside galleries and source code."""
import hashlib
import json
from pathlib import Path


def state_path(config):
    if config.get('family_state'):return Path(config['family_state'])
    if config.get('review_state'):return Path(config['review_state']).parent/'family-faces.json'
    return None


def load(config):
    path=state_path(config)
    data=json.loads(path.read_text(encoding='utf-8-sig')) if path and path.exists() else {'version':1,'items':{}}
    if data.get('version')!=1 or not isinstance(data.get('items'),dict):raise ValueError('Invalid family face ledger')
    from photo_triage.faces import vector
    for item in data['items'].values():
        for face in item.get('faces',[]):vector(face)
    return data


def revision(config):
    path=state_path(config)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path and path.exists() else 'no-family-references'


_encoders={}
def encoder(config):
    from photo_triage.faces import FaceEncoder
    folder=Path(config.get('family_models','C:/PhotoTools/models/family-faces'))
    key=str(folder.resolve())
    if key not in _encoders:_encoders[key]=FaceEncoder(folder)
    return _encoders[key]


def inspect(config,image,identity=None):
    from photo_triage.faces import matches
    data=load(config)
    exact=data['items'].get((identity or {}).get('sha256'))
    if exact and exact['bytes']==identity.get('bytes'):
        return {'matched':True,'source':'RESTORE_FAMILY','match_kind':'exact_reference','scope':'junk_and_tiktok_only'}
    refs=[face for item in data['items'].values() for face in item.get('faces',[])]
    if not refs:return {'matched':False,'status':'no_family_references'}
    return {**matches(encoder(config).encode(image),refs),'status':'checked',
            'source':'RESTORE_FAMILY','scope':'junk_and_tiktok_only','reference_revision':revision(config)}


def protected_result(evidence):
    return {'status':'analyzed','candidate':False,'category':None,'holds':[],
            'reason':'family_face_protected','family_protection':evidence}


def inspect_path(config,path):
    from PIL import Image,ImageOps
    import review_queue
    identity=review_queue.fresh(path)
    import pillow_heif
    pillow_heif.register_heif_opener()
    with Image.open(path) as image:
        result=inspect(config,ImageOps.exif_transpose(image).convert('RGB'),identity)
    if review_queue.fresh(path)!=identity:raise ValueError('Family comparison source changed')
    return result
