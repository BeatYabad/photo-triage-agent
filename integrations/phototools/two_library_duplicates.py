"""Exact-media deduplication: keep one verified copy in EACH gallery.

Default is a read-only plan (downloads cloud bytes when --download is supplied).
--apply removes exact extras within each gallery and reconciles verified gaps.
Verified recycling uses a current same-gallery keeper and original-path receipt.
Byte-different variants and unverified bundles are never merged by appearance.
"""
import argparse
from collections import Counter,defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import csv
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import subprocess
import time
import threading

import audit_two_libraries as audit
import merge_photos as media
import photo_settings as settings
from review_queue import FingerprintWorker,locked,load_state,write_index
import sync_local_photo_folders as sync
from live_photo_components import copy_hold, component_name
from windows_process_check import ensure_digikam_closed

RESERVE=50*1024**3

def write_json(path,data):
    temp=path.with_suffix(path.suffix+'.tmp')
    with temp.open('w',encoding='utf-8') as f:json.dump(data,f,indent=2);f.flush();os.fsync(f.fileno())
    for attempt in range(7):
        try:
            temp.replace(path)
            break
        except PermissionError as exc:
            # Windows readers/security scanners can briefly deny replacement.
            # Preserve the previous document and retry only known lock errors.
            if getattr(exc,'winerror',None) not in {5,32,33} or attempt==6:raise
            time.sleep(.05*(2**attempt)*random.uniform(1,1.2))

def scan_receipt(path,rows):
    write_json(path.with_name(path.name+'.complete.json'),{'complete':True,'records':len(rows),'file_sha256':media.sha256(path),'utc':sync.utc()})

def inventory(roots,database=None):
    mappings=sync.resource_map(database);rows=[];issues=[]
    for label,root in roots.items():
        if not root.is_dir():raise ValueError('Missing gallery: '+str(root))
        if any(p.is_symlink() or p.is_junction() for p in (root,*root.parents)):raise ValueError('Linked gallery')
        for directory,folders,files in os.walk(root,followlinks=False,onerror=lambda e:issues.append(str(e))):
            for name in list(folders):
                p=Path(directory)/name
                if name.casefold() in audit.SKIP|{'trash','$recycle.bin','shared'}:folders.remove(name);continue
                try:media.inside(p,root)
                except ValueError as e:folders.remove(name);issues.append(str(e))
            for name in files:
                p=Path(directory)/name;kind='video' if p.suffix.lower()=='.qt' else media.media_kind(p)
                if kind not in {'photo','video'}:continue
                try:
                    media.inside(p,root);st=p.stat()
                    row={'library':label,'root':str(root),'path':str(p),'kind':kind,
                         'bytes':st.st_size,'mtime_ns':st.st_mtime_ns,'status':'inventoried',
                         'placeholder':bool(getattr(st,'st_file_attributes',0)&audit.NONRESIDENT)}
                    records=mappings.get(str(p).casefold(),[]) if label=='icloud' else []
                    row['native_live_photo']=any(r.get('native_live_photo') for r in records)
                    if records and all(r['deleted'] for r in records):row['status']='excluded_deleted_asset'
                    elif records and not any(r['kind']=='resOriginalRes' and not r['deleted'] for r in records):row['status']='excluded_cached_rendition'
                    rows.append(row)
                except (OSError,ValueError) as e:issues.append(str(e))
    return rows,issues

def scan(roots,out,database=None,download=False,timeout=120,workers=4):
    rows,issues=inventory(roots,database);done=[];cloud=[]
    print(json.dumps({'phase':'inventory','files':len(rows),'placeholders':sum(r['placeholder'] for r in rows),
                      'placeholder_bytes':sum(r['bytes'] for r in rows if r['placeholder'])}),flush=True)
    if download:
        media.space(out,sum(r['bytes'] for r in rows if r['placeholder']),RESERVE)
    with (out/'files.jsonl').open('w',encoding='utf-8') as f:
        def record(row):
            done.append(row);f.write(json.dumps(row)+'\n')
            if len(done)%500==0:f.flush();print(json.dumps({'phase':'hashing','processed':len(done),'total':len(rows)}),flush=True)
        resident=[]
        for row in rows:
            if row['status'].startswith('excluded_'):record(row)
            elif row['placeholder']:cloud.append(row)
            else:resident.append(row)
        # Resident reads have no network retries. Placeholder reads are isolated
        # behind a kill deadline, with a circuit breaker for provider failures.
        local=threading.local();readers=[]
        def inspect_resident(row):
            if not hasattr(local,'reader'):
                local.reader=FingerprintWorker(timeout=min(timeout,300),allow_cloud=False);readers.append(local.reader)
            try:return {**row,**local.reader.read(row['path']),'status':'verified_local_bytes'}
            except (OSError,ValueError,subprocess.TimeoutExpired) as exc:return {**row,'status':'unreadable','error':str(exc)}
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for row in pool.map(inspect_resident,resident):record(row)
        finally:
            for resident_reader in readers:resident_reader.close()
        f.flush();print(json.dumps({'phase':'cloud_download','remaining':len(cloud)}),flush=True)
        worker=FingerprintWorker(timeout=min(timeout,300),allow_cloud=True);failures=0
        try:
            for n,row in enumerate(cloud,1):
                if not download:record({**row,'status':'placeholder_unverified'});continue
                if failures>=3:record({**row,'status':'provider_blocked','error':'Three consecutive download failures; provider circuit breaker'});continue
                try:
                    media.space(out,row['bytes'],RESERVE)
                    result=worker.read(row['path']);record({**row,**result,'status':'verified_local_bytes','download_verified':True});failures=0
                except (OSError,ValueError,subprocess.TimeoutExpired) as e:
                    record({**row,'status':'unreadable','error':str(e)});failures+=1
                    print(json.dumps({'phase':'download_error','path':row['path'],'error':str(e),'consecutive_failures':failures}),flush=True)
                    # No repeated attempt to the same failing file. Space apart
                    # consecutive provider probes with bounded exponential delay.
                    if failures<3:time.sleep(2**failures)
                if n%100==0:f.flush();print(json.dumps({'cloud_processed':n,'cloud_total':len(cloud),'download_failures':failures}),flush=True)
        finally:worker.close()
    scan_receipt(out/'files.jsonl',done)
    return done,issues

def groups(rows):
    result=defaultdict(lambda:defaultdict(list))
    for row in rows:
        if row['status']=='verified_local_bytes':result[(row['bytes'],row['sha256'])][row['library']].append(row)
    return result


def live_photo_component(row):
    """A matching image hash does not make a Live Photo a standalone still."""
    return bool(row.get('native_live_photo') or row.get('content_identifier')
                or row.get('metadata',{}).get('content_identifier'))

def distinct_named_assets(row,other,database):
    """Names locate candidates; current provider resource IDs establish relation."""
    if row.get('library')!='icloud' or not database:return False
    try:
        with closing(sqlite3.connect('file:'+Path(database).absolute().as_posix()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row;db.execute('BEGIN')
            from icloud_asset_record import mapped_row
            records=[mapped_row(db,Path(r['path'])) for r in (row,other)]
            for index,(item,record) in enumerate(zip((row,other),records)):
                if index==0 and record['asset_key']!='resOriginalRes':return False
                if record['filename'].casefold()!=Path(item['path']).name.casefold():return False
                if any(record[k] for k in ('is_deleted','is_expunged','is_deleted_locally')):return False
                if index==0 and any(record[k] for k in ('is_shared','has_edits')):return False
            return records[0]['asset_id']!=records[1]['asset_id']
    except (OSError,ValueError,sqlite3.Error):return False


def companion_hold(row,rows,database=None):
    hold=copy_hold(row)
    if hold:return hold
    p=Path(row['path']);stem=component_name(p)[0]
    candidates=rows.get((str(p.parent).casefold(),stem),[]) if isinstance(rows,dict) else rows
    for other in candidates:
        q=Path(other['path'])
        if q!=p and q.parent==p.parent and component_name(q)[0]==stem and q.suffix.casefold()!=p.suffix.casefold():
            if q.suffix.casefold() not in media.SIDECARS and distinct_named_assets(row,other,database):continue
            return 'motion_or_variant_companion'
    if any(q.exists() for ext in media.SIDECARS for q in (p.with_suffix(ext),p.with_name(p.name+ext))):return 'sidecar_companion'
    return None

def asset_evidence(row,keeper,database,delivery_ledger=None):
    if not database:raise ValueError('iCloud database required for asset-aware duplicate moves')
    with closing(sqlite3.connect('file:'+database.absolute().as_posix()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN')
        from icloud_asset_record import mapped_row
        evidence=[]
        for index,item in enumerate((row,keeper)):
            mapped=list(db.execute('SELECT s.*,f.asset_key FROM fingerprint_paths f JOIN server_items s ON f.fingerprint=s.asset_id WHERE lower(f.path)=lower(?)',(item['path'],)))
            if not mapped and index==0 and delivery_ledger:
                p=Path(item['path']);st=p.stat()
                receipts=[e for e in sync.read_lines(delivery_ledger) if e.get('action')=='queued_local_verified' and
                          e.get('destination','').casefold()==str(p).casefold() and e.get('target')=='icloud' and e.get('source_retained') and
                          (e.get('bytes'),e.get('sha256'),e.get('destination_mtime_ns'))==(item['bytes'],item['sha256'],st.st_mtime_ns)]
                if getattr(st,'st_file_attributes',0)&audit.NONRESIDENT:raise ValueError('Unmapped extra bytes unavailable')
                if not receipts:
                    # A standalone redundant local file can predate our receipts.
                    # Keep the mapped Apple original, and never infer absence of
                    # a native asset from its missing exact-path mapping alone.
                    registered=db.execute('SELECT asset_id FROM server_items WHERE lower(filename)=lower(?) AND dataclass_type IN (1,2) AND is_deleted=0 AND is_expunged=0 AND is_deleted_locally=0',(p.name,)).fetchone()
                    if registered:raise ValueError('Unmapped path has a native filename match; provider mapping pending')
                    if copy_hold(item):raise ValueError('Unmapped extra has component metadata')
                    identities=None
                    if getattr(st,'st_file_attributes',0)&0x400:
                        from cloud_photo_bytes import placeholder_identity
                        identities=[placeholder_identity(p),placeholder_identity(Path(keeper['path']))]
                        if identities[0]['sync_root_file_id']!=identities[1]['sync_root_file_id']:raise ValueError('Different provider roots; unmapped extra held')
                        if identities[0]['identity_sha256']==identities[1]['identity_sha256']:raise ValueError('Two paths share a provider identity; not independent duplicate assets')
                    evidence.append({'resource_key':'unmapped_exact_local_copy','asset_id':None,
                                     'file_attributes':getattr(st,'st_file_attributes',0),'provider_identities':identities})
                else:evidence.append({'local_upload_receipt':receipts[-1],'resource_key':'ordinary_import_copy','asset_id':None})
                continue
            if len(mapped)!=1:raise ValueError('No unique exact-path iCloud asset mapping')
            r=dict(mapped[0])
            if r['asset_key']!='resOriginalRes' or r['dataclass_type'] not in {1,2}:raise ValueError('Not a standalone original asset')
            if r['filename'].casefold()!=Path(item['path']).name.casefold():raise ValueError('Asset filename/path mismatch')
            if any(r[k] for k in ('is_deleted','is_expunged','is_deleted_locally','is_shared','has_edits')):raise ValueError('Edited/shared/deleted asset held')
            if index==0 and any(r[k] for k in ('alt_size','compl_size','full_size','full_vid_size')):raise ValueError('Additional source asset resources held')
            if r['orig_size']!=item['bytes']:raise ValueError('Asset original length mismatch')
            evidence.append({'asset_id':r['asset_id'],'master_id':r['master_id'],'resource_key':r['asset_key']})
        if evidence[0]['asset_id']==evidence[1]['asset_id']:raise ValueError('Two paths refer to one iCloud asset; not separate duplicates')
        return {'mode':'ordinary_import_copy_move' if evidence[0]['asset_id'] is None else 'native_recycle','resources':evidence}

def build_plan(rows,wanted,deleted,review_holds,database=None,delivery_ledger=None,pinned_keepers=None):
    content=groups(rows);moves=[];copies=[];holds=[];missing=[];distinct_assets=[]
    families=defaultdict(list)
    for r in rows:
        p=Path(r['path']);families[(str(p.parent).casefold(),component_name(p)[0])].append(r)
    unavailable=defaultdict(set)
    for r in rows:
        if r['status'] not in {'verified_local_bytes','excluded_deleted_asset','excluded_cached_rendition'}:unavailable[r['library']].add(r['bytes'])
    for (size,digest),libraries in sorted(content.items()):
        if digest in deleted-wanted:
            holds.append({'sha256':digest,'hold':'intentional_deletion','paths':[r['path'] for rs in libraries.values() for r in rs]});continue
        if digest in review_holds and digest not in wanted:
            holds.append({'sha256':digest,'hold':'pending_review','paths':[r['path'] for rs in libraries.values() for r in rs]});continue
        native_paths=set()
        if len(libraries.get('icloud',[]))>1 and database and Path(database).is_file():
            with closing(sqlite3.connect('file:'+Path(database).absolute().as_posix()+'?mode=ro',uri=True)) as db:
                for row in libraries['icloud']:
                    mapped=list(db.execute('SELECT s.asset_id FROM fingerprint_paths f JOIN server_items s ON f.fingerprint=s.asset_id WHERE lower(f.path)=lower(?) AND f.asset_key=\'resOriginalRes\' AND s.is_deleted=0 AND s.is_expunged=0 AND s.is_deleted_locally=0',(row['path'],)))
                    if len(mapped)==1:native_paths.add(row['path'])
        keepers={label:sorted(rs,key=lambda r:(label=='icloud' and r['path'] not in native_paths,
                    '__sha256-' in Path(r['path']).name,r['path'].casefold()))[0] for label,rs in libraries.items()}
        pinned=(pinned_keepers or {}).get(digest+':'+str(size),{})
        for label,path in pinned.items():
            if label in libraries:
                matches=[r for r in libraries[label] if r['path']==path]
                if len(matches)!=1:raise ValueError('Pinned duplicate keeper is unavailable')
                keepers[label]=matches[0]
        if len(libraries)!=2:
            label=next(iter(libraries));target='icloud' if label=='onedrive' else 'onedrive';source=keepers[label]
            hold='unavailable_same_size_possible_match' if size in unavailable[target] else companion_hold(source,families,database)
            copy={**source,'target':target,'hold':hold}
            copy['hold']=copy['hold'] or copy_hold(copy)
            copies.append(copy);missing.append({'sha256':digest,'bytes':size,'missing_library':target,'hold':copy['hold']})
        for label,rs in libraries.items():
            components=[r for r in rs if live_photo_component(r)]
            if components and len(rs)>1:
                distinct_assets.append({'library':label,'sha256':digest,'bytes':size,
                    'classification':'live_photo_components_are_distinct_from_standalone_files',
                    'live_photo_paths':[r['path'] for r in components],
                    'standalone_paths':[r['path'] for r in rs if not live_photo_component(r)]})
            rs=[r for r in rs if not live_photo_component(r)]
            if len(rs)<2:continue
            keeper=next((r for r in rs if r['path']==keepers[label]['path']),
                        sorted(rs,key=lambda r:r['path'].casefold())[0])
            for row in sorted(rs,key=lambda r:r['path'].casefold()):
                if row['path']==keeper['path']:continue
                hold=companion_hold(row,families,database)
                evidence=None
                if label=='icloud' and not hold:
                    try:evidence=asset_evidence(row,keeper,database,delivery_ledger)
                    # Provider mapping is optional context, not permission for
                    # removing a freshly verified exact local duplicate.
                    except (ValueError,OSError,sqlite3.Error):evidence=None
                move={**row,'keeper':keeper,'both_keepers':{**keepers,label:keeper},'hold':hold,'asset_evidence':evidence};moves.append(move)
                if hold:holds.append({'path':row['path'],'sha256':digest,'hold':hold})
    extras_by_gallery={label:sum(max(0,sum(not live_photo_component(r) for r in libraries.get(label,[]))-1) for libraries in content.values())
                       for label in ('onedrive','icloud')}
    return {'version':1,'moves':moves,'copies':copies,'missing':missing,'holds':holds,'distinct_assets':distinct_assets,
            'counts':{'unique_content':len(content),'extra_files':len(moves),'eligible_extra_files':sum(not r['hold'] for r in moves),
                      'distinct_live_photo_content_groups':len(distinct_assets),
                      'within_gallery_exact_extras':sum(extras_by_gallery.values()),'extras_by_gallery':extras_by_gallery,
                      'required_cross_gallery_matches':sum(len(libraries)==2 for libraries in content.values()),
                      'missing_content':len(missing),'eligible_copies':sum(not r['hold'] for r in copies),
                      'unverified_files':sum(r['status'] not in {'verified_local_bytes','excluded_deleted_asset','excluded_cached_rendition'} for r in rows)}}

def verify_row(reader,row,roots):
    root=roots[row['library']].resolve()
    if Path(row['root']).resolve()!=root:raise ValueError('Recorded root mismatch')
    media.inside(Path(row['path']),root)
    actual=reader.read(row['path'])
    if (actual['bytes'],actual['sha256'])!=(row['bytes'],row['sha256']):raise ValueError('Content changed: '+row['path'])
    return {**row,**actual}

def refresh(roots,out,database,previous,reader,retry_unavailable=False):
    """Inventory again; rehash new/changed files and retain unchanged scan proof.

    Selected sources, review copies, backups and required keepers additionally
    receive fresh full hashes before and after each move.
    """
    rows,issues=inventory(roots,database);old={r['path'].casefold():r for r in previous};result=[];cloud_failures=0
    for row in rows:
        prior=old.get(row['path'].casefold())
        if row['status'].startswith('excluded_'):result.append(row);continue
        if prior and prior.get('sha256') and prior['status']=='verified_local_bytes' and (row['bytes'],row['mtime_ns'])==(prior['bytes'],prior['mtime_ns']):
            result.append({**row,'sha256':prior['sha256'],'status':'verified_local_bytes','hash_evidence':'full_scan_hash_unchanged_size_mtime'})
        elif prior and not retry_unavailable and prior['status'] in {'unreadable','provider_blocked','placeholder_unverified'} and (row['bytes'],row['mtime_ns'])==(prior['bytes'],prior['mtime_ns']) and row['placeholder']==prior.get('placeholder',False):
            result.append({**row,'status':prior['status'],'error':prior.get('error'),'hash_evidence':'still_pending_no_blind_provider_retry'})
        elif row['placeholder'] and cloud_failures>=3:
            result.append({**row,'status':'provider_blocked','error':'Provider circuit breaker during refresh'})
        else:
            try:
                result.append({**row,**reader.read(row['path']),'status':'verified_local_bytes','hash_evidence':'fresh_full_hash'})
                if row['placeholder']:cloud_failures=0
            except (OSError,ValueError,subprocess.TimeoutExpired) as e:
                result.append({**row,'status':'unreadable','error':str(e)})
                if row['placeholder']:
                    cloud_failures+=1
                    if cloud_failures<3:time.sleep(2**cloud_failures)
    with (out/'post-files.jsonl').open('w',encoding='utf-8') as f:
        for row in result:f.write(json.dumps(row)+'\n')
    scan_receipt(out/'post-files.jsonl',result)
    return result,issues

def read_scan(path):
    rows=[]
    with path.open(encoding='utf-8-sig') as f:
        for line in f:
            if not line.endswith('\n'):raise ValueError('Interrupted scan record')
            rows.append(json.loads(line))
    receipt=path.with_name(path.name+'.complete.json')
    if receipt.exists():
        completed=json.loads(receipt.read_text(encoding='utf-8'))
        if not completed.get('complete') or completed['records']!=len(rows) or completed['file_sha256']!=media.sha256(path):
            raise ValueError('Incomplete or changed scan; rerun before applying')
    else:
        # Compatibility with earlier completed scans. New per-file receipts are
        # independent of summary.json, which changes after reconciliation.
        summary=json.loads((path.parent/'summary.json').read_text(encoding='utf-8'))
        if len(rows)!=summary['files']:raise ValueError('Incomplete scan; resume the scan before applying')
    return rows

def move_identity(row):
    return hashlib.sha256(row['path'].casefold().encode('utf-8')).hexdigest()[:20]

def save_duplicate_state(path,state):
    write_json(path,state)
    marker=path.with_suffix('.created')
    if not marker.exists():marker.write_text('Keep this marker with the duplicate ledger and independent backups.\n',encoding='utf-8')

def load_duplicate_state(path,review):
    if not path.exists():
        if path.with_suffix('.created').exists() or (review.exists() and any(review.rglob('*'))):raise ValueError('Duplicate review files/marker exist without ledger; recover provenance first')
        return {'version':1,'items':{}}
    state=json.loads(path.read_text(encoding='utf-8'))
    if state.get('version')!=1 or not isinstance(state.get('items'),dict):raise ValueError('Invalid duplicate ledger')
    for ident,item in state['items'].items():
        if ident!=item.get('id') or item.get('library') not in {'onedrive','icloud'} or item.get('status') not in {'prepared','move_intent','reviewing','recycled','restore_intent','restored'}:
            raise ValueError('Invalid duplicate receipt')
        if not isinstance(item.get('bytes'),int) or item['bytes']<0 or len(item.get('sha256',''))!=64:raise ValueError('Invalid duplicate content identity')
    return state

def recover_unsupported_rename(config,out,roots,reader):
    """Repair only a proven unstarted WinError 379 cross-provider rename.

    Native recycle intents and any existing destination remain blocked. This
    cannot authorize retrying an uncertain cloud deletion.
    """
    ensure_digikam_closed();path=out/'duplicate-state.json';state=load_duplicate_state(path,config['review_folder']/'DUPES')
    pending=[r for r in state['items'].values() if r['status'] in {'move_intent','restore_intent'}]
    if not pending:return 0
    failures=sync.read_lines(out/'failure-history.jsonl')
    if (out/'failure.json').exists():failures.append(json.loads((out/'failure.json').read_text(encoding='utf-8')))
    if len(pending)!=1 or not any(e.get('winerror')==379 and e.get('source')==pending[0]['source'] for e in failures):
        raise ValueError('Recovery limited to a recorded unsupported provider rename')
    item=pending[0]
    if item['status']!='move_intent' or item['library']!='icloud' or item['asset_evidence']['mode']!='ordinary_import_copy_move':raise ValueError('Uncertain cloud operation; manual recovery required')
    source=Path(item['source']);destination=Path(item['review_path']);backup=Path(item['backup'])
    media.inside(destination,config['review_folder']);media.inside(backup,config['preservation'])
    if destination.exists() or not source.exists():raise ValueError('Cannot prove the rename was unstarted')
    row={'path':str(source),'library':'icloud','root':item['root'],'sha256':item['sha256'],'bytes':item['bytes']}
    verify_row(reader,row,roots);actual=reader.read(backup)
    if (actual['bytes'],actual['sha256'])!=(item['bytes'],item['sha256']):raise ValueError('Recovery backup differs')
    for keeper in item['keepers'].values():verify_row(reader,keeper,roots)
    if asset_evidence(row,item['keepers']['icloud'],config['icloud_database'],config['delivery_ledger'])!=item['asset_evidence']:raise ValueError('Import/keeper evidence changed')
    media.journal(out/'actions.jsonl',{'action':'unsupported_rename_aborted_verified','source_intact':True,'review_destination_absent':True,**item})
    item['status']='prepared';save_duplicate_state(path,state);return 1

def apply_moves(plan,config,out,roots,reader,recycler=None,check=None):
    if not config.get('duplicate_cleanup_enabled',True):return {'duplicate_cleanup_disabled':1}
    if config.get('verified_no_backup_duplicates'):
        from verified_duplicate_recycle import apply
        return apply(plan,config,out,roots,reader,recycler,check)
    if os.name!='nt':raise ValueError('Windows no-overwrite move required')
    ensure_digikam_closed()
    state_path=out/'duplicate-state.json'
    state=load_duplicate_state(state_path,config['review_folder']/'DUPES')
    if any(item['status'] in {'move_intent','restore_intent'} for item in state['items'].values()):
        raise ValueError('Unresolved duplicate move/restore intent; inspect receipts or undo before another apply')
    stage=out/'staging';stage.mkdir(exist_ok=True)
    review=config['review_folder']/'DUPES';review.mkdir(parents=True,exist_ok=True)
    preservation=config['preservation']/'duplicates'
    from retention_policy import check_destination
    check_destination(preservation)
    preservation.mkdir(parents=True,exist_ok=True)
    counts=Counter();journal=out/'actions.jsonl'
    recycle=config.get('duplicate_action','review')=='recycle'
    for row in plan['moves']:
        if check:check()
        if row['hold']:continue
        # A keep decision protects content, not redundant exact paths.
        settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
        ensure_digikam_closed()
        ident=move_identity(row);old=state['items'].get(ident)
        if old:
            if old['source']!=row['path'] or old['sha256']!=row['sha256'] or old['bytes']!=row['bytes']:raise ValueError('Duplicate move identity changed')
            if old['status']!='prepared':raise ValueError('Previously moved/interrupted source returned; inspect duplicate receipt instead of repeating the move')
        source=Path(row['path']);media.inside(source,roots[row['library']])
        current=verify_row(reader,row,roots)
        for label,keeper in row['both_keepers'].items():
            if label!=keeper['library'] or keeper['path']==row['path']:raise ValueError('Invalid required keeper')
            verify_row(reader,keeper,roots)
        if row['library'] not in row['both_keepers']:raise ValueError('Same-gallery keeper required')
        if row['keeper']['library']!=row['library'] or row['both_keepers'][row['library']]['path']!=row['keeper']['path']:
            raise ValueError('Invalid same-gallery keeper')
        if row['library']=='icloud':
            if asset_evidence(current,row['keeper'],config.get('icloud_database'),config.get('delivery_ledger'))!=row['asset_evidence']:raise ValueError('iCloud asset evidence changed')
        if (recycle or row['library']=='icloud') and recycler is None:
            from delete_icloud_clutter import modern_recycler
            recycler=modern_recycler()
        from photo_names import choose
        destination=Path(old['review_path']) if old else choose(review,source.name,row['sha256'],row['bytes'],reuse=False)
        backup=Path(old['backup']) if old else choose(preservation,source.name,row['sha256'],row['bytes'])
        media.inside(destination,config['review_folder']);media.inside(backup,config['preservation'])
        item={'id':ident,'source':str(source),'library':row['library'],'root':row['root'],
              'review_path':str(destination),'backup':str(backup),'sha256':row['sha256'],'bytes':row['bytes'],
              'keepers':row['both_keepers'],'asset_evidence':row['asset_evidence'],'status':'prepared',
              'duplicate_action':'recycle' if recycle else 'review'}
        state['items'][ident]=item;save_duplicate_state(state_path,state)
        sync.atomic_copy({**current,'target':'preservation'},backup,config['preservation'],stage,journal,verify_hash=lambda p:reader.read(p)['sha256'])
        actual=reader.read(backup)
        if (actual['bytes'],actual['sha256'])!=(row['bytes'],row['sha256']):raise ValueError('Backup mismatch')
        icloud_move=row['library']=='icloud'
        if icloud_move and not recycle:
            sync.atomic_copy({**current,'target':'review'},destination,config['review_folder'],stage,journal,verify_hash=lambda p:reader.read(p)['sha256'])
        elif destination.exists():raise ValueError('Review destination occupied; no overwrite')
        # Verify keepers once more immediately before removing the extra source.
        verify_row(reader,current,roots)
        for keeper in row['both_keepers'].values():verify_row(reader,keeper,roots)
        settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
        item['status']='move_intent';save_duplicate_state(state_path,state);media.journal(journal,{'action':'duplicate_move_intent',**item})
        if not icloud_move and not recycle:source.rename(destination)
        else:recycler(str(source))
        verified_destination=Path(row['keeper']['path']) if recycle else destination
        actual=reader.read(verified_destination)
        if (actual['bytes'],actual['sha256'])!=(row['bytes'],row['sha256']) or source.exists():raise ValueError('Move verification failed; inspect receipt and backup')
        for keeper in row['both_keepers'].values():verify_row(reader,keeper,roots)
        item.update(status='recycled' if recycle else 'reviewing',moved_utc=sync.utc());save_duplicate_state(state_path,state)
        media.journal(journal,{'action':'duplicate_recycled_verified' if recycle else 'duplicate_moved_verified',
            **item,'destination':str(verified_destination),'permanent_delete':False,'cloud_verification':False})
        counts[('recycled_' if recycle else 'moved_')+row['library']]+=1
        if sum(counts.values())%20==0:print(json.dumps({'phase':'move','actions':dict(counts)}),flush=True)
    return dict(counts)

def reconcile(plan,config,out,roots,reader):
    ensure_digikam_closed();counts=Counter();stage=out/'staging';stage.mkdir(exist_ok=True)
    prior=defaultdict(set)
    for event in sync.read_lines(config['delivery_ledger']):
        if event.get('sha256') and event.get('bytes'):prior['icloud'].add((event['bytes'],event['sha256']))
    for event in sync.read_lines(out/'actions.jsonl'):
        if event.get('action')=='copy_intent' and event.get('target') in roots:prior[event['target']].add((event['bytes'],event['sha256']))
    for row in plan['copies']:
        # Plans may predate the current policy. Never backfill iCloud into OneDrive.
        if row.get('library')!='onedrive' or row.get('target')!='icloud':
            counts['reverse_copy_disabled']+=1;continue
        if row['hold']:continue
        wanted,deleted=settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
        if row['sha256'] in deleted-wanted or row['sha256'] in settings.review_move_hashes()-wanted:counts['decision_hold']+=1;continue
        if (row['bytes'],row['sha256']) in prior[row['target']]:counts['prior_delivery_hold']+=1;continue
        ensure_digikam_closed();current=verify_row(reader,row,roots)
        if copy_hold(current):counts['live_photo_component_hold']+=1;continue
        destination=sync.destination(current,roots[row['target']])
        sync.atomic_copy(current,destination,roots[row['target']],stage,out/'actions.jsonl',
                         config['delivery_ledger'] if row['target']=='icloud' else None,verify_hash=lambda p:reader.read(p)['sha256'])
        counts['copied_to_'+row['target']]+=1
        if sum(counts.values())%20==0:print(json.dumps({'phase':'reconcile','actions':dict(counts)}),flush=True)
    return dict(counts)

def undo(config,out,roots,reader):
    """Restore duplicate originals without marking any content permanently kept."""
    ensure_digikam_closed();state_path=out/'duplicate-state.json'
    state=load_duplicate_state(state_path,config['review_folder']/'DUPES');counts=Counter()
    stage=out/'staging';stage.mkdir(exist_ok=True)
    for item in state['items'].values():
        if item['status']=='restored':continue
        if item['status'] not in {'reviewing','recycled','move_intent','restore_intent'}:continue
        label=item['library'];source=Path(item['source']);review=Path(item['review_path']);backup=Path(item['backup'])
        if Path(item['root']).resolve()!=roots[label].resolve():raise ValueError('Restoration root mismatch')
        media.inside(source,roots[label]);media.inside(review,config['review_folder']);media.inside(backup,config['preservation'])
        is_recycled=item.get('duplicate_action')=='recycle'
        candidate=review if review.exists() and not is_recycled else backup
        actual=reader.read(candidate)
        if (actual['bytes'],actual['sha256'])!=(item['bytes'],item['sha256']):raise ValueError('Restore content mismatch')
        if label=='icloud' and item.get('asset_evidence',{}).get('mode')=='native_recycle':
            # Reuploading bytes creates a new cloud asset and can lose albums,
            # favorites or other native metadata. Restore the original asset in
            # Apple Recently Deleted first; then verify and clear this artifact.
            if not source.exists():counts['icloud_recently_deleted_recovery_pending']+=1;continue
            expected=item['asset_evidence']['resources'][0]['asset_id']
            with closing(sqlite3.connect('file:'+config['icloud_database'].as_posix()+'?mode=ro',uri=True)) as db:
                db.row_factory=sqlite3.Row
                from icloud_asset_record import mapped_row
                if mapped_row(db,source)['asset_id']!=expected:raise ValueError('Recovered iCloud asset identity differs')
        item['status']='restore_intent';save_duplicate_state(state_path,state)
        media.journal(out/'actions.jsonl',{'action':'duplicate_restore_intent',**item})
        sync.atomic_copy({'path':str(candidate),'root':str(config['review_folder'] if candidate==review else config['preservation']),
                          **actual,'target':label},source,roots[label],stage,out/'actions.jsonl',verify_hash=lambda p:reader.read(p)['sha256'])
        verify_row(reader,{'path':str(source),'root':str(roots[label]),'library':label,**actual},roots)
        if review.exists() and not is_recycled:
            media.inside(review,config['review_folder'])
            actual=reader.read(review)
            if (actual['bytes'],actual['sha256'])!=(item['bytes'],item['sha256']):raise ValueError('Review copy changed during restore')
            review.unlink() # only redundant review artifact, after exact original restored
        item.update(status='restored',restored_utc=sync.utc());save_duplicate_state(state_path,state)
        media.journal(out/'actions.jsonl',{'action':'duplicate_restored_verified',**item,'permanent_keep_decision':False})
        counts['restored_'+label]+=1
    return dict(counts)

def verify_batch(config,out,roots,reader):
    if config.get('verified_no_backup_duplicates') or (out/'verified-duplicate-state.json').exists():
        from verified_duplicate_recycle import verify
        return verify(out,roots,reader)
    """Independent full-byte audit of receipts, review copies and both keepers."""
    state=load_duplicate_state(out/'duplicate-state.json',config['review_folder']/'DUPES');moved=0;copied=0;pending=[]
    for item in state['items'].values():
        if item['status']=='restored':continue
        if item['status'] not in {'reviewing','recycled'}:pending.append(item['id']);continue
        media.inside(Path(item['source']),roots[item['library']])
        if Path(item['source']).exists():raise ValueError('Moved duplicate source returned: '+item['source'])
        preserved=[('backup',config['preservation'])]
        if item['status']=='reviewing':preserved.append(('review_path',config['review_folder']))
        for key,root in preserved:
            p=Path(item[key]);media.inside(p,root);actual=reader.read(p)
            if (actual['bytes'],actual['sha256'])!=(item['bytes'],item['sha256']):raise ValueError('Receipt content mismatch: '+str(p))
        if item['library'] not in item['keepers']:raise ValueError('Same-gallery keeper not recorded')
        for keeper in item['keepers'].values():verify_row(reader,keeper,roots)
        moved+=1
    copies={}
    for event in sync.read_lines(out/'actions.jsonl'):
        if event['action']=='copy_verified' and event.get('target') in roots:copies[(event['destination'],event['sha256'])]=event
    for event in copies.values():
        target=event['target'];origin='icloud' if target=='onedrive' else 'onedrive'
        for p,label in [(event['source'],origin),(event['destination'],target)]:
            verify_row(reader,{'path':p,'library':label,'root':str(roots[label]),'bytes':event['bytes'],'sha256':event['sha256']},roots)
        copied+=1
    wanted,_=settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
    result={'status':'all_batch_receipts_verified' if not pending else 'unresolved_intents',
            'moved_review_backup_and_both_keepers_full_hash_verified':moved,'missing_copy_source_and_destination_full_hash_verified':copied,
            'unresolved':pending,'safe_hashes_preserved':len(wanted),'permanent_keep_decisions_added':0,'cloud_equality_verified':False,'utc':sync.utc()}
    # Reuse the established human CSV formatter; this projection never becomes
    # an executor ledger or a permanent keep decision.
    write_index(out/'duplicate-state.json',{'items':{ident:{
        'review_path':item['review_path'],'status':item['status'],'representative':{'path':item['source']},
        'origins':[{'path':item['source']},*item['keepers'].values()],
        'evidence':[{'reasons':['exact_duplicate_extra']}],
        'quality_move':{'retained_duplicates':list(item['keepers'].values())}}
        for ident,item in state['items'].items()}})
    write_json(out/'verification.json',result);return result

def report(out,rows,issues,plan,actions=None):
    summary={'status':'pending' if issues or plan['missing'] or plan['holds'] or plan['counts']['unverified_files'] or plan['counts']['extra_files'] else 'one_copy_in_each_local_library',
             'files':len(rows),'file_statuses':dict(Counter(r['status'] for r in rows)),**plan['counts'],
             'holds_by_reason':dict(Counter(r['hold'] for r in plan['holds'])),'issues':issues,
             'actions':actions or {},'cloud_equality_verified':False,'comparison':'Full SHA-256 plus byte length; distinct encodings/edits retained'}
    events=sync.read_lines(out/'actions.jsonl')
    summary['cumulative_verified_actions']={
        'missing_copies_created':len({(e['destination'],e['sha256']) for e in events if e['action']=='copy_verified' and e.get('target') in {'onedrive','icloud'}}),
        'duplicate_moves':len({e['id'] for e in events if e['action']=='duplicate_moved_verified'}),
        'duplicates_recycled':len({e['id'] for e in events if e['action']=='duplicate_recycled_verified'})}
    write_json(out/'summary.json',summary);write_json(out/'plan.json',plan)
    with (out/'duplicates.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['library','extra_file','retained_in_same_library','sha256','bytes','hold'])
        for r in plan['moves']:w.writerow([r['library'],r['path'],r['keeper']['path'],r['sha256'],r['bytes'],r['hold'] or ''])
    (out/'summary.md').write_text('# Two-library duplicates\n\n'+json.dumps(summary,indent=2)+'\n\nSee duplicates.csv for extra files and their retained copies. See plan.json for missing copies and holds. Only freshly verified exact bytes qualify. Required cross-library copies remain. No permanent deletion or keep decision. Local folder verification does not verify remote accounts.\n',encoding='utf-8')
    print(json.dumps(summary),flush=True);return summary

def validate_layout(config,out):
    roots={k:config['roots'][k] for k in ('onedrive','icloud')}
    paths=[*roots.values(),config['review_folder'],config['preservation'],out]
    for i,p in enumerate(paths):
        if any(q.is_symlink() or q.is_junction() for q in (p,*p.parents)):raise ValueError('Linked work path')
        for q in paths[i+1:]:
            if p==q or p in q.parents or q in p.parents:raise ValueError('Overlapping gallery/review/preservation/output paths')
    if any(part.casefold().startswith(('onedrive','icloud')) for part in out.parts):raise ValueError('Reports must be outside sync roots')
    return roots

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--download',action='store_true',help='Read/download placeholders with bounded deadlines; never trust hash filenames')
    p.add_argument('--timeout',type=float,default=120)
    p.add_argument('--output',type=Path)
    p.add_argument('--delete-extras',action='store_true',help='Recycle exact extras automatically; preserve one copy within each gallery')
    p.add_argument('--duplicates-only',action='store_true',help='Remove within-gallery extras without copying cross-gallery gaps')
    p.add_argument('--from-scan',type=Path,help='Reuse a completed full scan; refresh inventory and fully rehash selected sources/keepers')
    p.add_argument('--retry-downloads',action='store_true',help='After fixing the sync client, retry pending downloads from a completed scan with the same circuit breaker')
    p.add_argument('--recover-unsupported-rename',action='store_true',help='Recover only a proven unstarted WinError 379 iCloud import-copy rename before applying')
    mode=p.add_mutually_exclusive_group();mode.add_argument('--apply',action='store_true');mode.add_argument('--undo',action='store_true');mode.add_argument('--verify',action='store_true')
    a=p.parse_args()
    if not 5<=a.timeout<=600:p.error('timeout must be 5..600 seconds')
    if a.retry_downloads and (not a.download or not a.from_scan):p.error('retry-downloads requires --download and --from-scan')
    if a.recover_unsupported_rename and not a.apply:p.error('recover-unsupported-rename requires --apply')
    config=settings.load()
    if not config.get('duplicate_cleanup_enabled',True):
        print(json.dumps({'status':'disabled','reason':'Duplicate cleanup handled by digiKam'}));return 0
    wanted,deleted=settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
    if a.delete_extras:config['duplicate_action']='recycle'
    os.environ.setdefault('PHOTOS_CONFIG',str(settings.DEFAULT.resolve()))
    load_state(config['review_state'])
    out=(a.output or config['output']/'duplicates').resolve();roots=validate_layout(config,out);out.mkdir(parents=True,exist_ok=True)
    with locked(out/'duplicate-state.json'):
        reader=FingerprintWorker(timeout=a.timeout,allow_cloud=a.download)
        try:
            if a.verify:
                verified=verify_batch(config,out,roots,reader);print(json.dumps(verified));return 0 if not verified['unresolved'] else 2
            if a.undo:
                restored=undo(config,out,roots,reader);print(json.dumps(restored));return 2 if restored.get('icloud_recently_deleted_recovery_pending') else 0
            if a.apply:ensure_digikam_closed()
            if a.from_scan:
                rows,issues=refresh(roots,out,config.get('icloud_database'),read_scan(a.from_scan.resolve()),reader,a.retry_downloads)
            else:rows,issues=scan(roots,out,config.get('icloud_database'),a.download,a.timeout)
            plan=build_plan(rows,wanted,deleted,settings.review_move_hashes()-wanted,config.get('icloud_database'),config['delivery_ledger'])
            report(out,rows,issues,plan)
            actions={}
            if a.apply:
                if issues:raise ValueError('Inventory issues must be resolved before mutation')
                if a.recover_unsupported_rename:actions['verified_unstarted_rename_recovered']=recover_unsupported_rename(config,out,roots,reader)
                write_json(out/'applied-plan.json',plan)
                actions.update(apply_moves(plan,config,out,roots,reader))
                rows,issues=refresh(roots,out,config.get('icloud_database'),rows,reader)
                wanted,deleted=settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
                plan=build_plan(rows,wanted,deleted,settings.review_move_hashes()-wanted,config.get('icloud_database'),config['delivery_ledger'])
                if not a.duplicates_only:
                    actions.update(reconcile(plan,config,out,roots,reader))
                    rows,issues=refresh(roots,out,config.get('icloud_database'),rows,reader)
                    plan=build_plan(rows,wanted,deleted,settings.review_move_hashes()-wanted,config.get('icloud_database'),config['delivery_ledger'])
            summary=report(out,rows,issues,plan,actions)
        except Exception as e:
            failure={'error':str(e),'type':type(e).__name__,'winerror':getattr(e,'winerror',None),'source':getattr(e,'filename',None),
                     'utc':sync.utc(),'action':'Inspect duplicate-state.json and actions.jsonl; do not repeat unresolved move intents'}
            media.journal(out/'failure-history.jsonl',failure);write_json(out/'failure.json',failure)
            raise
        finally:reader.close()
    return 0 if summary['status']=='one_copy_in_each_local_library' else 2

if __name__=='__main__':raise SystemExit(main())
