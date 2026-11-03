"""Copy one exact-content representative for review; RESTORE means keep forever.

Originals are retained. Missing review files never authorize source deletion.
All origins are recorded. SHA-256 keep decisions survive renames and duplicates.
"""
import argparse
import atexit
from contextlib import contextmanager
import csv
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

VALID_STATUSES={'copy_pending','reviewing','restore_pending','family_restore_pending','kept','delete_pending','deleted'}

def load_state(path):
    path=Path(path)
    if not path.exists():
        if path.with_suffix('.created').exists():raise ValueError('Review ledger missing; recover it before processing')
        return {'version':1,'items':{}}
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    if data.get('version')!=1 or not isinstance(data.get('items'),dict):raise ValueError('Invalid review ledger')
    for digest,row in data['items'].items():
        if not re.fullmatch('[0-9a-f]{64}',digest) or row.get('status') not in VALID_STATUSES:
            raise ValueError('Invalid review decision')
        if not isinstance(row.get('origins'),list) or (not row['origins'] and row.get('origin_kind')!='unknown_review'):raise ValueError('Missing review provenance')
        for source in row['origins']:
            if source.get('sha256')!=digest or source.get('label') not in {'icloud','onedrive','amazon','import','folder'} or not isinstance(source.get('bytes'),int):
                raise ValueError('Invalid original content identity')
    expected=sorted(h for h,r in data['items'].items() if r['status'] in {'kept','restore_pending'})
    if data.get('excluded_sha256')!=expected:raise ValueError('Review keep index is inconsistent')
    return data

def save_state(path,data):
    path=Path(path);temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    data['excluded_sha256']=sorted(h for h,r in data['items'].items() if r['status'] in {'kept','restore_pending'})
    with temporary.open('x',encoding='utf-8') as f:
        json.dump(data,f,indent=2);f.flush();os.fsync(f.fileno())
    for attempt in range(6):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt==5:raise
            time.sleep(.1 * 2**attempt)
    marker=path.with_suffix('.created')
    if not marker.exists():
        with marker.open('x') as f:f.write('Keep this marker and the review ledger together.\n');f.flush();os.fsync(f.fileno())
def write_index(path,data):
    """Human-readable provenance; never an executor input or core-state write."""
    path=Path(path)
    index=path.parent/'review-index.csv';temp_index=index.with_name(index.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp_index.open('x',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['review_file','status','preferred_source','original_locations','reasons','review_path','retained_duplicates','Script reason'])
        for item in data['items'].values():
            cells=[Path(item['review_path']).name,item['status'],item['representative']['path'],
                   ' | '.join(r['path'] for r in item['origins']),
                   ' | '.join(sorted({str(reason) for r in item.get('evidence',[]) for reason in r.get('reasons',[r.get('category') or 'review'])})),
                   item['review_path'],' | '.join(r['path'] for r in item.get('quality_move',{}).get('retained_duplicates',[])),
                   item.get('script_reason','')]
            writer.writerow(["'"+v if v.startswith(('=','+','-','@','\t','\r','\n')) else v for v in cells])
        f.flush();os.fsync(f.fileno())
    temp_index.replace(index)

def register_clutter_move(state_path, entry, review_path, backup):
    """Record verified preservation before a reviewed clutter source is recycled.

    The caller freshly verifies the source and both distinct preserved copies.
    This also imports verified historical executor receipts, without inventing
    origins from a filename or treating a review as a permanent keep decision.
    """
    digest=entry['sha256'];review_path=Path(review_path);state_path=Path(state_path)
    with locked(state_path):
        data=load_state(state_path);item=data['items'].get(digest)
        if item and item['status']!='reviewing':raise ValueError('Review decision already exists; reconcile before moving')
        if item and Path(item['review_path']).resolve()!=review_path.resolve():
            raise ValueError('Content already has a different review copy')
        source={k:entry[k] for k in ('sha256','bytes','mtime_ns','path','root','label')}
        if item and any(row['bytes']!=entry['bytes'] for row in item['origins']):raise ValueError('Review identity differs')
        if item is None:
            item={'status':'reviewing','origins':[],'representative':source,'review_path':str(review_path),
                  'created_utc':__import__('scan_reference_photos').utc(),'evidence':[]}
            data['items'][digest]=item
        if not any(row['path'].casefold()==source['path'].casefold() for row in item['origins']):
            item['origins'].append(source)
        if source['label']=='onedrive':item['representative']=source
        item['clutter_move']={'independent_copy':str(backup),'journaled_originals':True}
        evidence={k:entry[k] for k in ('category','reason','visual_evidence','reviewed_utc') if k in entry}
        if evidence not in item['evidence']:item['evidence'].append(evidence)
        save_state(state_path,data);write_index(state_path,data)
    return item

def fingerprint(path,allow_cloud=False):
    """Child-process entry: locally resident bytes only, before/after stat guard."""
    import merge_photos as media
    import scan_reference_photos as scan
    path=Path(path)
    if any(scan.linked(p) for p in (path,*path.parents)):raise ValueError('Linked file')
    before=path.stat()
    if getattr(before,'st_file_attributes',0)&scan.NONRESIDENT and not allow_cloud:raise ValueError('Nonresident placeholder')
    if allow_cloud and getattr(before,'st_file_attributes',0)&scan.NONRESIDENT:
        from cloud_photo_bytes import hydrate
        hydrate(path)
    digest=hashlib.sha256();length=0
    with path.open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):digest.update(block);length+=len(block)
    after=path.stat()
    if length!=before.st_size or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('File changed or incomplete while hashing')
    return {'sha256':digest.hexdigest(),'bytes':after.st_size,'mtime_ns':after.st_mtime_ns}

def fingerprint_worker(connection,allow_cloud=False):
    """Reuse one isolated process; its parent retains a per-file kill deadline."""
    try:
        while True:
            path=connection.recv()
            if path is None:break
            try:result={'result':fingerprint(path,allow_cloud)}
            except Exception as exc:result={'error':type(exc).__name__+': '+str(exc)}
            connection.send(result)
    except (EOFError,BrokenPipeError):pass
    finally:connection.close()

class FingerprintWorker:
    def __init__(self,timeout=45,allow_cloud=False):
        self.timeout=timeout;self.allow_cloud=allow_cloud;self.connection=None;self.process=None

    def close(self):
        if self.process:
            if self.process.is_alive():self.process.terminate()
            self.process.join(3)
            self.process=None
        if self.connection:self.connection.close();self.connection=None

    def read(self,path):
        if self.process is None:
            parent,child=mp.get_context('spawn').Pipe()
            self.process=mp.get_context('spawn').Process(target=fingerprint_worker,args=(child,self.allow_cloud),daemon=True)
            self.process.start();child.close();self.connection=parent
        try:
            from file_deadline import exchange
            try:response=exchange(self.connection,min(self.timeout,300),str(path))
            except TimeoutError:raise subprocess.TimeoutExpired('photo fingerprint',min(self.timeout,300))
        except (OSError,EOFError,subprocess.TimeoutExpired):
            self.close();raise
        if 'error' in response:raise ValueError(response['error'])
        return response['result']

_fingerprints=None

def fresh(path):
    global _fingerprints
    if _fingerprints is None:
        # Hashing retains the user's five-minute file-operation budget, including
        # large videos checked before/after family matching.
        _fingerprints=FingerprintWorker(timeout=300);atexit.register(_fingerprints.close)
    return _fingerprints.read(path)

def origin(path,label,root):
    import merge_photos as media
    media.inside(path,root)
    return {'path':str(path),'label':label,'root':str(root),**fresh(path)}


def registered_folder_root(state_path, root):
    """Only a folder explicitly registered by process-folder can be restored."""
    root=Path(root).resolve();registry=Path(state_path).parent/'folder-roots.json'
    data=json.loads(registry.read_text(encoding='utf-8'))
    if data.get('version')!=1 or str(root) not in data.get('roots',[]):
        raise ValueError('External source folder is not registered; recover its processing receipts')
    return root


def register_folder_root(state_path, root):
    import scan_reference_photos as scan
    root=Path(root).resolve();state_path=Path(state_path)
    if not root.is_dir() or root==Path(root.anchor) or any(scan.linked(p) for p in (root,*root.parents)):
        raise ValueError('Invalid folder-processing source root')
    with locked(state_path):
        registry=state_path.parent/'folder-roots.json'
        data=json.loads(registry.read_text(encoding='utf-8')) if registry.exists() else {'version':1,'roots':[]}
        if data.get('version')!=1 or not isinstance(data.get('roots'),list):raise ValueError('Invalid folder registry')
        if str(root) not in data['roots']:
            data['roots'].append(str(root))
            temporary=registry.with_suffix('.tmp')
            with temporary.open('w',encoding='utf-8') as stream:
                json.dump(data,stream,indent=2);stream.flush();os.fsync(stream.fileno())
            temporary.replace(registry)

def records(path):
    rows={}
    with Path(path).open(encoding='utf-8-sig') as f:
        for line in f:
            if not line.endswith('\n'):raise ValueError('Incomplete scan record; rerun scanner before collection')
            row=json.loads(line)
            if row.get('path'):rows[row['path']]=row
    return list(rows.values())

def layout(roots,state_path,review,restore):
    import scan_reference_photos as scan
    paths=[state_path,review,restore]
    if review==restore or review in restore.parents or restore in review.parents:raise ValueError('Review and RESTORE must be separate')
    for p in paths:
        if any(scan.linked(q) for q in (p,*p.parents)):raise ValueError('Linked review path')
        if any(p==r or r in p.parents or p in r.parents for r in roots.values()):raise ValueError('Review data must be separate from galleries')
        if p==state_path and any(part.casefold().startswith(('onedrive','icloud')) for part in p.parts):raise ValueError('Keep review state outside synchronized folders')
    if state_path==review or state_path==restore or review in state_path.parents or restore in state_path.parents:
        raise ValueError('Keep review state beside the folders, not inside them')
    if not state_path.exists() and any(p.exists() and any(p.iterdir()) for p in (review,restore)):
        raise ValueError('Review files exist without their ledger; recover the ledger first')
    for p in roots.values():
        if not p.is_dir() or any(scan.linked(q) for q in (p,*p.parents)):raise ValueError('Missing or linked gallery')

@contextmanager
def locked(state_path):
    state_path.parent.mkdir(parents=True,exist_ok=True)
    lock=state_path.with_suffix('.lock')
    with lock.open('x') as f:f.write(str(os.getpid()))
    try:yield
    finally:lock.unlink(missing_ok=True)

def collect(scan_path,roots,state_path,review,stage,journal,wanted,wanted_loader=None,progress=None,inventory_rows=None,eligible_hashes=None,remember_keep=True):
    import merge_photos as media
    import scan_reference_photos as scan
    from sync_local_photo_folders import atomic_copy
    data=load_state(state_path);candidates={};held=[];counts={}
    from photo_names import choose
    reserved={os.path.abspath(r['review_path']).casefold():(h,r['representative']['bytes']) for h,r in data['items'].items() if r['status']!='kept'}
    for row in records(scan_path):
        if not row.get('candidate') or row.get('status') not in {'analyzed','analyzed_with_sample_gaps'}:continue
        h=row.get('sha256','')
        if eligible_hashes is not None and h not in eligible_hashes:continue
        if not re.fullmatch('[0-9a-f]{64}',h) or row.get('label') not in {'icloud','onedrive','amazon','import','folder'}:raise ValueError('Invalid candidate provenance')
        if row['label'] not in roots:continue
        legacy=row.get('rule','').startswith(('quality-metrics-','reference-clip-','nsfw-public-api-','nsfw-marqo-'))
        unified=row.get('rule','').startswith('unified-pass-v1-') and row.get('analysis_check') in {'quality','junk','nsfw','tiktok'}
        if not (legacy or unified):raise ValueError('Unknown screening rule')
        media.inside(Path(row['path']),roots[row['label']])
        if h not in wanted and h not in data['items']:candidates.setdefault((h,row['bytes']),[]).append(row)
    # Discover all exact copies, even when a limited scan did not visit OneDrive.
    sizes={size for h,size in candidates};groups={key:[] for key in candidates}
    if inventory_rows is None:rows,issues=scan.inventory(roots,scan.PHOTOS|__import__('nsfw_analysis').VIDEOS)
    else:
        rows=[];issues=[]
        for item in inventory_rows:
            label=item.get('label',item.get('library'))
            if label not in roots or item.get('kind') not in {'photo','video','sidecar'}:continue
            if Path(item['root']).resolve()!=roots[label].resolve():raise ValueError('Completed pass root differs from configuration')
            if item['status'] in {'excluded_deleted_asset','excluded_cached_rendition'}:continue
            rows.append({**item,'label':label})
    held.extend(issues)
    for number,row in enumerate(rows,1):
        if progress:progress('duplicate_discovery',number,len(rows),len(held),0)
        if row['bytes'] not in sizes:continue
        try:
            found=origin(Path(row['path']),row['label'],roots[row['label']])
            if inventory_rows is not None and row.get('sha256') and any(found[k]!=row[k] for k in ('sha256','bytes','mtime_ns')):
                raise ValueError('Recorded origin changed since completed pass')
            key=(found['sha256'],found['bytes'])
            if key in groups:groups[key].append(found)
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
            held.append({'path':row['path'],'label':row['label'],'bytes':row['bytes'],'error':str(exc)})
    # An unreadable same-sized OneDrive file could be the preferred duplicate.
    for number,(key,origins) in enumerate(groups.items(),1):
        if progress:progress('recording_provenance',number,len(groups),len(held),0)
        if any(r.get('label')=='onedrive' and (r.get('bytes')==key[1] or 'bytes' not in r) for r in held):
            held.append({'sha256':key[0],'error':'Preferred gallery has an unresolved possible duplicate'});continue
        if not origins:held.append({'sha256':key[0],'error':'No unchanged resident original'});continue
        chosen=min(origins,key=lambda r:(r['label']!='onedrive',r['label'],r['path'].casefold()))
        h=key[0];dest=choose(review,Path(chosen['path']).name,h,key[1],reserved)
        data['items'][h]={'status':'copy_pending','origins':origins,'representative':chosen,
                         'review_path':str(dest),'created_utc':scan.utc(),
                         'evidence':[{k:v for k,v in evidence.items() if k in {
                             'path','label','sha256','bytes','category','analysis_check','rule','reasons',
                             'reason','target_score','margin','confidence_band','metrics'}} for evidence in candidates[key]]}
        save_state(state_path,data) # Durable provenance precedes publication.
    # Recover interrupted publication using the same destination, never another copy.
    for number,(h,item) in enumerate(data['items'].items(),1):
        if progress:progress('copying_review',number,len(data['items']),len(held),sum(counts.values()))
        if item['status']!='copy_pending':continue
        if eligible_hashes is not None and h not in eligible_hashes:continue
        if item['representative']['label'] not in roots:continue
        if h in (wanted_loader() if wanted_loader else wanted):continue
        try:
            chosen=item['representative'];dest=Path(item['review_path']);media.inside(dest,review)
            media.inside(Path(chosen['path']),roots[chosen['label']])
            if Path(chosen['root']).resolve()!=roots[chosen['label']]:raise ValueError('Gallery configuration changed; review provenance first')
            current=fresh(Path(chosen['path']))
            if current['sha256']!=h or current['bytes']!=chosen['bytes']:raise ValueError('Selected original changed')
            result=atomic_copy({**chosen,**current,'target':'review'},dest,review,stage,journal)
            item['status']='reviewing';item['copied_utc']=scan.utc();save_state(state_path,data)
            counts[result]=counts.get(result,0)+1
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:held.append({'sha256':h,'error':str(exc)})
    if progress:progress('finished',len(data['items']),len(data['items']),len(held),sum(counts.values()))
    return {'actions':counts,'held':held,'reviewing':sum(r['status']=='reviewing' for r in data['items'].values()),'source_deletions':0}

def relocate_review(roots,state_path,old_review,review,restore,stage,journal,wanted_loader,progress=None):
    """Copy/verify, persist the new path, then remove only the old review copy.

    A pending relocation is durable before publication so interrupted runs resume
    without inventing provenance or interpreting a missing file as deletion intent.
    """
    import merge_photos as media
    import scan_reference_photos as scan
    from sync_local_photo_folders import atomic_copy
    old_review=Path(old_review).resolve()
    layout(roots,state_path,old_review,restore)
    if old_review==review or old_review in review.parents or review in old_review.parents:
        raise ValueError('Old and new review folders must be separate')
    data=load_state(state_path);held=[];moved=0
    for number,(h,item) in enumerate(data['items'].items(),1):
        if item['status']!='reviewing':continue
        pending=item.get('relocation_pending')
        source=Path(pending['source'] if pending else item['review_path']).resolve()
        if not pending and source.parent==review:continue # Already finished.
        try:
            if h in wanted_loader():raise ValueError('Reviewed-wanted hash: relocation held')
            # Direct queue entries only; never accept an original or nested path.
            if source.parent!=old_review:raise ValueError('Recorded copy is outside the old review folder')
            media.inside(source,old_review)
            dest=review/source.name;media.inside(dest,review)
            if pending and Path(pending['destination']).resolve()!=dest:raise ValueError('Pending relocation destination changed')
            expected=item['representative']['bytes']
            if source.exists():
                current=fresh(source)
                if (current['sha256'],current['bytes'])!=(h,expected):raise ValueError('Review copy changed')
            elif not pending:raise ValueError('Review copy missing; no deletion intent inferred')
            if not pending:
                item['relocation_pending']={'source':str(source),'destination':str(dest)}
                save_state(state_path,data)
                media.journal(journal,{'action':'review_relocation_intent','sha256':h,'source':str(source),'destination':str(dest)})
            if source.exists():
                atomic_copy({'path':str(source),'root':str(old_review),**current,'target':'review'},dest,review,stage,journal)
            verified=fresh(dest)
            if (verified['sha256'],verified['bytes'])!=(h,expected):raise ValueError('Relocated copy verification failed')
            item['review_path']=str(dest);save_state(state_path,data)
            # Only the recorded old review artifact is removed, after durable state
            # and fresh hashes of both copies. Library origins are never touched.
            if source.exists():
                current=fresh(source)
                if (current['sha256'],current['bytes'])!=(h,expected):raise ValueError('Old copy changed before removal')
                if h in wanted_loader():raise ValueError('Reviewed-wanted hash: old copy retained')
                source.unlink()
            item.pop('relocation_pending',None);item['relocated_utc']=scan.utc();save_state(state_path,data)
            media.journal(journal,{'action':'review_relocation_verified','sha256':h,'source':str(source),
                                  'destination':str(dest),'bytes':expected,'originals_retained':True})
            moved+=1
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:held.append({'sha256':h,'error':str(exc)})
        if progress:progress('relocating_review',number,len(data['items']),len(held),moved)
    if progress:progress('finished',len(data['items']),len(data['items']),len(held),moved)
    return {'relocated_verified':moved,'held':held,'source_deletions':0,
            'reviewing':sum(r['status']=='reviewing' for r in data['items'].values())}

def restore_gallery_index(root, sizes):
    """Fresh OneDrive comparison once per RESTORE batch; no cached path evidence."""
    from photo_pass import inventory
    rows,issues=inventory({'onedrive':root},{})
    if issues:raise ValueError('OneDrive restore inventory incomplete: '+str(issues))
    matches={};unavailable=set();reader=FingerprintWorker()
    try:
        for row in rows:
            if row['bytes'] not in sizes:continue
            if row['placeholder']:unavailable.add(row['bytes']);continue
            try:
                actual=reader.read(Path(row['path']))
                matches.setdefault((actual['sha256'],actual['bytes']),[]).append(Path(row['path']))
            except (OSError,ValueError,subprocess.TimeoutExpired):unavailable.add(row['bytes'])
    finally:reader.close()
    return matches,unavailable


def restore_recorded_locations(roots,source,item,journal):
    """Known provenance restores exact locations; a conflict is never overwritten."""
    from privacy_storage import verify,copy_verified,cloud_placeholder
    import merge_photos as media
    targets=[]
    for original in item['origins']:
        label=original['label'];root=roots.get(label)
        if root is None and label=='folder':
            root=registered_folder_root(Path(journal).parent/'review-state.json',original['root'])
        recorded=Path(original['root']).resolve()
        # Selecting a parent intake folder must not invalidate exact provenance
        # from a previous import of its subtree. Never admit unrelated roots.
        if label=='import' and root is not None and recorded.is_relative_to(Path(root).resolve()):
            if recorded!=Path(root).resolve():media.inside(recorded,Path(root))
            root=recorded
        if root is None or recorded!=Path(root).resolve():
            raise ValueError('Recorded original root differs; retain RESTORE for recovery')
        if (original.get('sha256'),original.get('bytes'))!=(source['sha256'],source['bytes']):
            raise ValueError('Recorded original identity differs from RESTORE bytes')
        target=Path(original['path']);media.inside(target,Path(root))
        if target.exists() and not cloud_placeholder(target):verify(target,source['sha256'],source['bytes'])
        targets.append((target,Path(root)))
    # Validate all occupied locations before publishing the first missing one.
    for target,root in targets:
        copy_verified(Path(source['path']),target,root,source['sha256'],source['bytes'],restore_placeholder=True)
        verify(target,source['sha256'],source['bytes'])
        media.journal(journal,{'action':'restore_original_verified','destination':str(target),
                              'sha256':source['sha256'],'bytes':source['bytes']})
    return [path for path,_ in targets]


def deliver_restore_to_gallery(roots,source,item,stage,journal,index):
    from privacy_storage import verify,copy_verified
    from photo_names import choose,original_name
    import organize_dates as dates
    import merge_photos as media
    root=roots['onedrive'];key=(source['sha256'],source['bytes']);matches,unavailable=index
    existing=sorted(matches.get(key,[]),key=lambda p:str(p).casefold())
    if existing:
        target=existing[0];verify(target,*key);result='existing_exact_verified'
    else:
        if source['bytes'] in unavailable:raise ValueError('Same-sized OneDrive originals unavailable; download before adding a possible duplicate')
        source_path=Path(source['path']);name=original_name(item)
        stamp,_,_=dates.choose_date(source_path,Path(name),'none')
        stamp=stamp or dates.named_date(Path(name).stem)
        if stamp is None:stamp,_,_=dates.choose_date(source_path,Path(name),'mtime')
        if stamp is None:raise ValueError('No organization date; RESTORE retained')
        directory=root/stamp.strftime('%Y/%m')
        target=choose(directory,name,*key)
        media.journal(journal,{'action':'restore_gallery_copy_intent','sha256':key[0],'bytes':key[1],
                              'source':source['path'],'destination':str(target),'target':'onedrive'})
        copy_verified(source_path,target,root,*key);result='copied_verified'
        verify(target,*key);matches.setdefault(key,[]).append(target)
    media.journal(journal,{'action':'restore_gallery_verified','sha256':key[0],'bytes':key[1],
                          'destination':str(target),'result':result})
    return target


def restore_files(roots,state_path,restore,stage,journal,inventory_rows=None,eligible_hashes=None,remember_keep=True,family_only=False):
    import merge_photos as media
    import scan_reference_photos as scan
    from privacy_storage import copy_verified,verify
    from photo_names import choose,original_name
    data=load_state(state_path);held=[];kept=0;cleared=0;inputs={};previously_kept=set();restored_destinations={}
    pending_status='family_restore_pending' if family_only else 'restore_pending'
    files,issues=scan.inventory({'restore':restore},scan.PHOTOS|__import__('nsfw_analysis').VIDEOS|set(media.SIDECARS)) if inventory_rows is None else (inventory_rows,[])
    held.extend(issues)
    for file in files:
        try:
            source=origin(Path(file['path']),'restore',restore);h=source['sha256']
            if eligible_hashes is not None and h not in eligible_hashes:continue
            item=data['items'].get(h)
            if not item:
                item={'status':'restore_pending','origin_kind':'unknown_review','origins':[],
                      'representative':source,'review_path':str(source['path']),'evidence':[]}
                data['items'][h]=item
            if item['status']=='kept':previously_kept.add(h)
            if not remember_keep:item['restore_recheck']=True
            if h in previously_kept:item['restore_preserve_prior_keep']=True
            inputs.setdefault(h,[]).append(source)
            item['status']=pending_status;item['restore_source']=source;save_state(state_path,data)
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:held.append({'path':file['path'],'error':str(exc)})
    # Finish pending keeps after interruption even if input filenames were changed.
    pending=[(h,item) for h,item in data['items'].items() if item['status']==pending_status and (eligible_hashes is None or h in eligible_hashes)]
    unknown=[item for _,item in pending if not item['origins']]
    try:index=restore_gallery_index(roots['onedrive'],{item['representative']['bytes'] for item in unknown}) if unknown else ({},set())
    except (OSError,ValueError,KeyError) as exc:
        held.append({'error':str(exc)})
        return {'new_kept_hashes':kept,'restore_files_cleared':cleared,'held':held,'source_deletions':0,'cloud_coverage_verified':False}
    for h,item in list(data['items'].items()):
        if item['status']!=pending_status:continue
        if eligible_hashes is not None and h not in eligible_hashes:continue
        try:
            source=item['restore_source'];media.inside(Path(source['path']),restore)
            source={**source,**fresh(Path(source['path']))}
            if (source['sha256'],source['bytes'])!=(h,item['representative']['bytes']):raise ValueError('RESTORE content changed')
            targets=restore_recorded_locations(roots,source,item,journal) if item['origins'] else [deliver_restore_to_gallery(roots,source,item,stage,journal,index)]
            dest=targets[0]
            target_root=next((r['root'] for r in item['origins'] if r['path']==str(dest)),None)
            if target_root is None:
                if 'onedrive' not in roots:raise ValueError('Unknown restore origin requires OneDrive fallback')
                target_root=str(roots['onedrive'])
            item['restore_destinations']=[str(path) for path in targets]
            restored_destinations[h]=item['restore_destinations']
            item['restore_destination']={'path':str(dest),'root':target_root,
                                         'sha256':h,'bytes':source['bytes'],'status':'local_verified_cloud_pending'}
            item['restore_backup']=None
            keep=not item.get('restore_recheck') or item.get('restore_preserve_prior_keep',False)
            if keep:item['status']='kept';item['kept_utc']=scan.utc()
            if item.get('nsfw_privacy'):item['nsfw_privacy'].update(status='cancelled_by_restore',cancelled_utc=scan.utc())
            save_state(state_path,data)
            media.journal(journal,{'action':'keep_hash_recorded' if keep else 'restore_for_recheck_verified','sha256':h,'origins':item['origins'],'destinations':[str(p) for p in targets]})
            kept+=keep and h not in previously_kept
            for artifact in inputs.get(h,[source]):
                path=Path(artifact['path']);media.inside(path,restore)
                if not path.exists():continue
                for target in targets:verify(target,h,source['bytes'])
                verify(path,h,source['bytes'])
                media.journal(journal,{'action':'restore_review_cleanup_intent','sha256':h,'path':str(path),'backup':None})
                path.unlink();cleared+=1
                media.journal(journal,{'action':'restore_review_cleared','sha256':h,'path':str(path),'backup':None})
            if not keep:
                data['items'].pop(h,None);save_state(state_path,data)
        except (OSError,ValueError,KeyError,subprocess.TimeoutExpired) as exc:held.append({'sha256':h,'error':str(exc)})
    return {'new_kept_hashes':kept,'restore_files_cleared':cleared,'held':held,'source_deletions':0,'cloud_coverage_verified':False,'restored_destinations':restored_destinations}

def main():
    if len(sys.argv)==3 and sys.argv[1]=='_fingerprint':
        print(json.dumps(fingerprint(sys.argv[2])));return 0
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['collect','restore','status','relocate'])
    p.add_argument('--root',action='append',required=True)
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--review',type=Path,required=True)
    p.add_argument('--restore',type=Path,required=True)
    p.add_argument('--exclusions',type=Path,action='append',required=True)
    p.add_argument('--scan',type=Path)
    p.add_argument('--manifest',type=Path,help='Completed combined-pass files.jsonl; reuse recorded origins instead of recrawling galleries')
    p.add_argument('--from-review',type=Path,help='Old review folder; relocation changes only recorded review copies')
    a=p.parse_args();roots={k:Path(v).resolve() for k,v in (r.split('=',1) for r in a.root)}
    if not roots or not set(roots)<={'icloud','onedrive','amazon','import'}:p.error('Select supported local galleries or IMPORT')
    if a.action=='restore' and not {'icloud','onedrive','amazon'}<=set(roots):p.error('Restore requires all configured galleries to honor recorded origins')
    state=a.state.resolve();review=a.review.resolve();restore=a.restore.resolve()
    layout(roots,state,review,restore)
    if len(a.exclusions)<2:p.error('Both reviewed-wanted ledgers are required')
    from photo_settings import hashes
    wanted=hashes(a.exclusions)
    if a.action=='status':
        from collections import Counter
        print(json.dumps(dict(Counter(r['status'] for r in load_state(state)['items'].values())),indent=2));return 0
    if a.action=='collect' and not a.scan:p.error('--scan is required')
    if a.action=='relocate' and not a.from_review:p.error('--from-review is required')
    from windows_process_check import ensure_digikam_closed
    if a.action=='restore':ensure_digikam_closed()
    with locked(state):
        review.mkdir(parents=True,exist_ok=True);restore.mkdir(parents=True,exist_ok=True)
        stage=state.parent/'.staging';stage.mkdir(exist_ok=True);journal=state.parent/'review-actions.jsonl'
        last_report=[0.0]
        def progress(phase,done,total,holds,copied):
            now=time.monotonic()
            if now-last_report[0]<15 and phase!='finished':return
            last_report[0]=now
            payload={'phase':phase,'processed':done,'total':total,'holds':holds,'copied':copied,
                     'selected_galleries':list(roots)}
            temporary=state.parent/'collect-progress.tmp'
            temporary.write_text(json.dumps(payload,indent=2),encoding='utf-8')
            temporary.replace(state.parent/'collect-progress.json')
            print(json.dumps(payload),flush=True)
        if a.action=='collect':
            from two_library_duplicates import read_scan
            manifest=read_scan(a.manifest) if a.manifest else None
            result=collect(a.scan,roots,state,review,stage,journal,wanted,lambda:hashes(a.exclusions),progress,manifest)
        elif a.action=='relocate':result=relocate_review(roots,state,a.from_review,review,restore,stage,journal,lambda:hashes(a.exclusions),progress)
        else:result=restore_files(roots,state,restore,stage,journal)
        result['review_folder']=str(review);result['restore_folder']=str(restore)
        result['selected_galleries']=list(roots)
        try:write_index(state,load_state(state))
        except OSError as exc:result['index_warning']=str(exc) # Excel may hold the report open.
        (state.parent/'review-summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result,indent=2));return 2 if result['held'] else 0

if __name__=='__main__':raise SystemExit(main())
