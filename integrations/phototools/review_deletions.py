"""DELETE is an explicit exact-content deletion request for both local galleries."""
import json
from pathlib import Path
import subprocess
import re
from collections import defaultdict
import review_queue as queue
from two_library_duplicates import write_json


def intents(config):
    path=config['review_state'].parent/'delete-requests.json'
    data=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'version':1,'items':{}}
    if data.get('version')!=1 or not isinstance(data.get('items'),dict):raise ValueError('Invalid delete request ledger')
    for digest,item in data['items'].items():
        if not re.fullmatch('[0-9a-f]{64}',digest) or type(item.get('bytes')) is not int or item['bytes']<0:
            raise ValueError('Invalid delete request identity')
    return data


def exclude(config,digest,enabled=True):
    path=config['deletion_ledger']
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    values=set(data.get('folder_deleted_sha256',[]))
    if enabled:
        values.add(digest)
        data['excluded_import_sha256']=sorted(set(data['excluded_import_sha256'])|{digest})
    else:values.discard(digest)
    data['folder_deleted_sha256']=sorted(values)
    write_json(path,data)


def execute(config,requests,rows,recycler=None,include_completed=False):
    # The shared lock also serializes RESTORE and CONFIRMED decisions against
    # removal. Inventory and intake have released it before this boundary.
    with queue.locked(config['review_state']):
        result=_execute_locked(config,requests,rows,recycler,include_completed)
        prune_delete_folders(config,result)
        return result


def prune_delete_folders(config,result):
    from empty_folders import prune
    folder=config.get('delete_folder',config['review_folder'].parent/'DELETE')
    result.setdefault('removed_empty_folders',[]).extend(prune(folder))


def _execute_locked(config,requests,rows,recycler=None,include_completed=False):
    from delete_icloud_clutter import modern_recycler
    from privacy_storage import verify
    from windows_process_check import ensure_digikam_closed
    import merge_photos as media
    result={'removed_gallery_files':0,'cleared_delete_files':0,'held':[],
            'cloud_deletion_verified':False}
    if not requests['items']:return result
    recycler=recycler or modern_recycler()
    journal=config['review_state'].parent/'review-delete-actions.jsonl'
    state=queue.load_state(config['review_state'])
    active=set(config.get('delete_galleries',config.get('active_galleries',['onedrive','icloud'])))
    selected=config.get('_delete_selected_hashes')
    sizes={item['bytes'] for digest,item in requests['items'].items()
           if (selected is None or digest in selected) and item.get('status')!='cancelled_by_restore' and (include_completed or item.get('status')=='pending')}
    index=defaultdict(list);unreadable=defaultdict(list);families=defaultdict(list)
    from live_photo_components import component_name
    def family(row):
        path=Path(row['path'])
        return (str(path.parent).casefold(),component_name(path)[0])
    for row in rows:
        families[family(row)].append(row)
        if row.get('label',row.get('library')) not in active or row['bytes'] not in sizes:continue
        path=Path(row['path'])
        if not path.exists():continue
        try:
            actual=queue.fresh(path)
            index[(actual['sha256'],actual['bytes'])].append({**row,**actual})
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
            unreadable[row['bytes']].append({'path':str(path),'error':str(exc)})
    for digest,item in requests['items'].items():
        if selected is not None and digest not in selected:continue
        try:
            current=state['items'].get(digest,{})
            if current.get('status')=='restore_pending' or (current.get('status')=='kept' and current.get('kept_utc','')>item.get('requested_utc','')):
                raise ValueError('A newer RESTORE decision blocks deletion')
            if item.get('status')=='cancelled_by_restore':continue
            if item.get('status')!='pending' and not include_completed:continue
            # Compare each candidate once per batch, then reverify selected
            # bytes at every removal boundary below. Same-size requests share
            # the comparison work, not mutation authorization.
            size=item['bytes'];matches=index.get((digest,size),[]);unavailable=unreadable.get(size,[])
            # DELETE explicitly authorizes recycling. No retained backup store.
            # Validate inbox bytes and containment before removing any matches.
            sources=[Path(p) for p in item.get('inbox_paths',[]) if Path(p).exists()]
            for path in sources:
                media.inside(path,config.get('delete_folder',config['review_folder'].parent/'DELETE'))
                verify(path,digest,size)
            from sync_local_photo_folders import copy_hold
            motion_plans={}
            for row in matches:
                hold=copy_hold(row,families[family(row)])
                if hold=='live_photo_requires_complete_asset_plan':
                    from approved_bundle_delete import validate_member
                    if validate_member(config,row,digest):hold=None
                    if hold:
                        from delete_motion_plan import explicit_motion
                        plan=explicit_motion(config,row)
                        if plan:
                            motion_plans[row['path']]=plan
                            hold=None
                if hold and row.get('label')=='icloud' and config.get('icloud_database'):
                    # A shared basename/embedded association is not proof of an
                    # actual native bundle. Require authoritative standalone evidence.
                    from icloud_asset_record import standalone_record
                    try:
                        standalone_record(Path(row['path']),config['roots']['icloud'],config['icloud_database'])
                        hold=None
                    except (OSError,ValueError,RuntimeError):
                        pass
                if hold:raise ValueError('Delete requires a complete companion plan: '+hold)
            for row in matches:
                path=Path(row['path']);media.inside(path,config['roots'][row['label']])
                ensure_digikam_closed();verify(path,digest,size)
                plan=motion_plans.get(row['path'])
                event={'path':str(path),'sha256':digest,'bytes':size,'backup':None,'removal':'recycle_bin','gallery':row['label']}
                if plan:event['explicit_motion_delete']=plan
                media.journal(journal,{'action':'review_delete_intent',**event})
                recycler(str(path))
                if path.exists():raise ValueError('Gallery deletion incomplete')
                media.journal(journal,{'action':'review_deleted_verified',**event})
                result['removed_gallery_files']+=1
            item['deferred_galleries']=[k for k in ('onedrive','icloud') if k not in active]
            unavailable=[*unavailable,*[{'gallery':label,'error':'Matching gallery action deferred: gallery not inventoried'}
                                       for label in item['deferred_galleries']]]
            item['status']='pending' if unavailable else 'deleted_locally_verified'
            item['unavailable']=unavailable
            result['held'].extend(unavailable)
            if not unavailable:
                for path in sources:
                    verify(path,digest,size)
                    media.journal(journal,{'action':'delete_inbox_clear_intent','path':str(path),'sha256':digest,'backup':None,'removal':'recycle_bin'})
                    recycler(str(path))
                    if path.exists():raise ValueError('DELETE inbox removal incomplete')
                    media.journal(journal,{'action':'delete_inbox_cleared_verified','path':str(path),'sha256':digest,'bytes':size,'removal':'recycle_bin'})
                    result['cleared_delete_files']+=1
            write_json(config['review_state'].parent/'delete-requests.json',requests)
            if not unavailable and digest in state['items']:
                state['items'][digest]['status']='deleted'
                queue.save_state(config['review_state'],state)
        except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
            result['held'].append({'sha256':digest,'error':str(exc)})
    return result


def service(config,recycler=None,restore_hashes=None):
    result=_service(config,recycler,restore_hashes)
    prune_delete_folders(config,result)
    write_json(config['output']/'review-delete-report.json',result)
    return result


def _service(config,recycler=None,restore_hashes=None):
    """RESTORE wins conflicts. Only DELETE intake/pending work crawls galleries."""
    from photo_pass import inventory
    import merge_photos as media
    folder=config.get('delete_folder',config['review_folder'].parent/'DELETE')
    folder.mkdir(parents=True,exist_ok=True)
    requests=intents(config)
    # A content deletion applies to both galleries even when the initiating
    # processing task is scoped to one. Import delivery remains iCloud-only.
    active=['onedrive','icloud']
    config={**config,'delete_galleries':active}
    for request in requests['items'].values():
        if set(request.get('deferred_galleries',[])) & set(active):request['status']='pending'
    result={'removed_gallery_files':0,'cleared_delete_files':0,'held':[],'gallery_crawls':0,'cloud_deletion_verified':False}
    # A successful later RESTORE supersedes an older DELETE decision.
    state=queue.load_state(config['review_state'])
    import family_faces
    family=family_faces.load(config)['items']
    for digest,item in requests['items'].items():
        kept=state['items'].get(digest,{})
        family_restore=family.get(digest,{})
        if (kept.get('status')=='kept' and kept.get('kept_utc','')>item.get('requested_utc','')) or (
                family_restore.get('bytes')==item.get('bytes') and family_restore.get('restored_utc','')>item.get('requested_utc','')):
            item['status']='cancelled_by_restore';exclude(config,digest,False)
    current_hashes=set()
    restore_hashes=set(restore_hashes or ())
    for path in config['restore_folder'].rglob('*'):
        if path.is_file():
            try:restore_hashes.add(queue.fresh(path)['sha256'])
            except (OSError,ValueError,subprocess.TimeoutExpired):pass
    with queue.locked(config['review_state']):
        state=queue.load_state(config['review_state'])
        for path in folder.rglob('*'):
            if not path.is_file():continue
            try:
                media.inside(path,folder);actual=queue.fresh(path);digest=actual['sha256']
                if digest in restore_hashes:raise ValueError('RESTORE and DELETE contain the same content; RESTORE takes precedence')
                item=requests['items'].setdefault(digest,{'bytes':actual['bytes'],'inbox_paths':[]})
                if item['bytes']!=actual['bytes']:raise ValueError('Delete identity differs')
                current_hashes.add(digest)
                item.update(status='pending',requested_utc=media.utc())
                if str(path) not in item['inbox_paths']:item['inbox_paths'].append(str(path))
                exclude(config,digest)
                if digest in state['items']:
                    state['items'][digest]['status']='delete_pending'
                    state['items'][digest].pop('nsfw_confirmation',None)
                media.journal(config['review_state'].parent/'review-delete-actions.jsonl',
                    {'action':'user_requested_delete_by_folder','path':str(path),'sha256':digest,'bytes':actual['bytes']})
            except (OSError,ValueError,subprocess.TimeoutExpired) as exc:result['held'].append({'path':str(path),'error':str(exc)})
        queue.save_state(config['review_state'],state)
        write_json(config['review_state'].parent/'delete-requests.json',requests)
    # A phone/provider may return previously deleted content. The explicit
    # request remains authoritative; freshly compare it on subsequent runs.
    if config.get('review_delete_current_inbox_only'):
        config={**config,'_delete_selected_hashes':current_hashes}
    pending={h:r for h,r in requests['items'].items() if r.get('status') in {'pending','deleted_locally_verified'}
             and (not config.get('review_delete_current_inbox_only') or h in current_hashes)}
    if not pending:
        write_json(config['output']/'review-delete-report.json',result)
        return result
    if config.get('defer_review_deletions'):
        result['pending_requests']=len(pending)
        result['status']='queued_for_combined_pass'
        write_json(config['output']/'review-delete-report.json',result)
        return result
    from live_photo_cleanup import native_assets
    mappings=native_assets(config['icloud_database']) if config.get('icloud_database') else {}
    rows,issues=inventory({k:config['roots'][k] for k in active},mappings,include_unknown=True)
    result['gallery_crawls']=len(active)
    unresolved=[]
    # Include unavailable regular files by current byte length. They hold only
    # requests of that length, rather than stopping unrelated DELETE requests.
    for issue in issues:
        try:
            if not isinstance(issue,dict) or not issue.get('path'):raise ValueError('Unknown inventory gap')
            path=Path(issue['path']);stat=path.stat()
            if not path.is_file():raise ValueError('Inventory gap is not a regular file')
            label=next(k for k in active if path.is_relative_to(config['roots'][k]))
            rows.append({'path':str(path),'label':label,'library':label,'root':str(config['roots'][label]),
                         'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,'kind':media.category(path)})
        except (OSError,ValueError,StopIteration):unresolved.append(issue)
    if unresolved:
        result['held'].extend({'error':str(issue)} for issue in unresolved);return result
    completed=execute(config,requests,rows,recycler,include_completed=not config.get('review_delete_pending_only',False))
    write_json(config['review_state'].parent/'delete-requests.json',requests)
    result.update({k:v for k,v in completed.items() if k!='held'});result['held'].extend(completed['held'])
    with queue.locked(config['review_state']):
        state=queue.load_state(config['review_state'])
        for digest,item in pending.items():
            if digest in state['items'] and item['status']=='deleted_locally_verified':state['items'][digest]['status']='deleted'
        queue.save_state(config['review_state'],state)
    write_json(config['output']/'review-delete-report.json',result)
    return result
