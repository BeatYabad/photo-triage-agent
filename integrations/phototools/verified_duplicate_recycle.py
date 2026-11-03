"""Recycle exact gallery extras after current keeper verification, without backups."""
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def recycle(path,timeout):
    subprocess.run([getattr(sys,'_base_executable',sys.executable),str(Path(__file__).resolve()),str(path),str(Path(sys.prefix)/'Lib'/'site-packages')],
                   check=True,capture_output=True,timeout=max(.001,min(300,timeout)),
                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def apply(plan,config,out,roots,reader,recycler=None,check=None):
    import two_library_duplicates as duplicates
    from merge_photos import journal,inside
    duplicates.ensure_digikam_closed()
    state_path=out/'verified-duplicate-state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {'version':1,'items':{}}
    if any(item['status']=='intent' for item in state['items'].values()):
        raise ValueError('Unresolved recycle intent; inspect current keeper and Recycle Bin before retry')
    counts=Counter();held=[]
    for row in plan['moves']:
        if check:check()
        if row.get('hold'):continue
        started=time.monotonic();ident=duplicates.move_identity(row)
        try:
            if ident in state['items']:raise ValueError('Previously processed duplicate; refusing repeated action')
            duplicates.ensure_digikam_closed()
            source=Path(row['path']);keeper=row['keeper'];target=Path(keeper['path'])
            if duplicates.live_photo_component(row) or duplicates.live_photo_component(keeper):
                raise ValueError('Live Photo components are distinct assets, not standalone duplicates')
            if keeper['library']!=row['library'] or source==target:
                raise ValueError('Distinct same-gallery keeper required')
            if (keeper['sha256'],keeper['bytes'])!=(row['sha256'],row['bytes']):
                raise ValueError('Keeper identity differs from duplicate')
            inside(source,roots[row['library']]);inside(target,roots[row['library']])
            if source.samefile(target):raise ValueError('Source and keeper are physical aliases')
            current=duplicates.verify_row(reader,row,roots)
            duplicates.verify_row(reader,keeper,roots)
            # Recheck current siblings and provider relationships at removal,
            # including files that appeared after the plan was produced.
            for member in (row,):
                stem=duplicates.component_name(Path(member['path']))[0]
                siblings=[{'path':str(p),'library':member['library']} for p in Path(member['path']).parent.iterdir()
                          if duplicates.component_name(p)[0]==stem and p.is_file()]
                hold=duplicates.companion_hold(member,siblings,config.get('icloud_database'))
                if hold:raise ValueError('Current companion relationship: '+hold)
            # Local exact-duplicate cleanup does not require cloud asset mapping.
            # Fresh bytes, distinct paths and companion checks govern removal.
            duplicates.verify_row(reader,row,roots)
            duplicates.verify_row(reader,keeper,roots)
            if time.monotonic()-started>=300:raise TimeoutError('Duplicate file deadline exceeded')
            item={'id':ident,'source':str(source),'destination':str(target),'library':row['library'],
                  'root':row['root'],'sha256':row['sha256'],'bytes':row['bytes'],
                  'keeper':keeper,'backup':None,'retention':'verified_destination_only',
                  'asset_evidence':row.get('asset_evidence'),'status':'intent'}
            state['items'][ident]=item;duplicates.write_json(state_path,state)
            journal(out/'actions.jsonl',{'action':'duplicate_move_intent',**item})
            if recycler:recycler(str(source))
            else:recycle(source,300-(time.monotonic()-started))
            if source.exists():raise ValueError('Recycle did not remove the source')
            duplicates.verify_row(reader,keeper,roots)
            item['status']='recycled';duplicates.write_json(state_path,state)
            journal(out/'actions.jsonl',{'action':'duplicate_recycled_verified',**item,
                    'permanent_delete':False,'cloud_verification':False})
            counts['recycled_'+row['library']]+=1
        except (OSError,ValueError,TimeoutError,subprocess.SubprocessError) as exc:
            held.append({'path':row['path'],'error':str(exc)})
    duplicates.write_json(out/'duplicate-holds.json',held)
    return dict(counts)


def verify(out,roots,reader):
    from two_library_duplicates import verify_row
    path=out/'verified-duplicate-state.json'
    state=json.loads(path.read_text()) if path.exists() else {'items':{}}
    verified=0;pending=[]
    for item in state['items'].values():
        if item['status']!='recycled':pending.append(item['id']);continue
        if Path(item['source']).exists():raise ValueError('Recycled source returned')
        verify_row(reader,item['keeper'],roots);verified+=1
    holds_path=out/'duplicate-holds.json'
    holds=json.loads(holds_path.read_text()) if holds_path.exists() else []
    return {'status':'all_batch_receipts_verified' if not pending and not holds else 'held',
            'verified_destination_only':verified,'unresolved':pending,'held':holds}


if __name__=='__main__':
    import site
    site.addsitedir(sys.argv[2])
    from delete_icloud_clutter import modern_recycler
    modern_recycler()(sys.argv[1])
