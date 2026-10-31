"""Restore user-supplied family photos and enroll unnamed local face references."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import family_faces
import photo_settings as settings
import review_queue as queue
import scan_reference_photos as scan
from two_library_duplicates import write_json


def encode_file(config,path):
    command=[sys.executable,str(Path(__file__).resolve()),'--encode',str(path),
             '--model-folder',str(config.get('family_models','C:/PhotoTools/models/family-faces'))]
    result=subprocess.run(command,capture_output=True,text=True,timeout=300,
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise RuntimeError('Family enrollment failed: '+result.stderr[-1500:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def process(config):
    folder=Path(config.get('family_restore_folder',config['restore_folder'].parent/'RESTORE_FAMILY'))
    result={'restored':0,'enrolled_faces':0,'held':[],'enrollment_gaps':[]}
    prior=family_faces.load(config)
    result['enrollment_gaps']=[{'sha256':digest,'reason':'No usable face; add a clearer reference for these people'}
        for digest,item in prior['items'].items() if item.get('status')=='no_usable_face']
    if not folder.exists():return result
    rows,issues=scan.inventory({'restore':folder},scan.PHOTOS)
    result['held'].extend(issues)
    unsupported=[p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() not in scan.PHOTOS]
    result['held'].extend({'path':str(p),'error':'RESTORE_FAMILY accepts photos; this file was left in place'} for p in unsupported)
    if not rows:return result
    from windows_process_check import ensure_digikam_closed
    from PIL import Image,ImageOps
    import pillow_heif
    pillow_heif.register_heif_opener()
    ensure_digikam_closed()
    with queue.locked(config['review_state']):
        bank=family_faces.load(config)
        for row in rows:
            try:
                source={**row,**queue.fresh(row['path'])};digest=source['sha256']
                prior=bank['items'].get(digest)
                if prior and prior['bytes']!=source['bytes']:raise ValueError('Family reference identity mismatch')
                if prior and prior.get('faces'):
                    faces=prior['faces']
                else:
                    faces=encode_file(config,source['path'])
                    if queue.fresh(source['path'])['sha256']!=digest:raise ValueError('Family input changed during enrollment')
                    bank['items'][digest]={'sha256':digest,'bytes':source['bytes'],'faces':faces,
                        'status':'enrolled' if faces else 'no_usable_face','utc':scan.utc()}
                    write_json(family_faces.state_path(config),bank)
                    result['enrolled_faces']+=len(faces)
                import reaudit_nsfw
                item=queue.load_state(config['review_state'])['items'].get(digest,{'origins':[]})
                bank['items'][digest]['origins']=item.get('origins',[])
                roots=reaudit_nsfw.recorded_roots(config,item,source)
                restored=queue.restore_files(roots,config['review_state'],folder,None,
                    config['review_state'].parent/'review-actions.jsonl',inventory_rows=[source],eligible_hashes={digest},remember_keep=False)
                result['restored']+=restored['restore_files_cleared'];result['held'].extend(restored['held'])
                kept=queue.load_state(config['review_state'])['items'].get(digest,{})
                bank['items'][digest]['restore_destinations']=restored.get('restored_destinations',{}).get(digest,kept.get('restore_destinations',[]))
                if restored['restore_files_cleared']:bank['items'][digest]['restored_utc']=scan.utc()
                write_json(family_faces.state_path(config),bank)
                if not faces and not any(g['sha256']==digest for g in result['enrollment_gaps']):
                    result['enrollment_gaps'].append({'sha256':digest,'reason':'Restored and protected from junk by exact bytes; no usable face for protecting other photos'})
            except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError) as exc:
                result['held'].append({'path':row['path'],'error':str(exc)})
    write_json(config['review_state'].parent/'restore-family-report.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=settings.DEFAULT)
    parser.add_argument('--encode',type=Path)
    parser.add_argument('--model-folder',type=Path)
    args=parser.parse_args()
    if args.encode:
        from PIL import Image,ImageOps
        import pillow_heif
        pillow_heif.register_heif_opener()
        from photo_triage.faces import FaceEncoder
        with Image.open(args.encode) as im:
            faces=FaceEncoder(args.model_folder).encode(ImageOps.exif_transpose(im).convert('RGB'))
        print(json.dumps(faces));return 0
    result=process(settings.load(args.config));print(json.dumps(result),flush=True)
    return 2 if result['held'] or result['enrollment_gaps'] else 0


if __name__=='__main__':raise SystemExit(main())
