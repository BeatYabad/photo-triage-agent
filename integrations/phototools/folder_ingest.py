"""One scoped model pass, then verified review/sorting/Apple-folder delivery.

Only the supplied Camera Roll subtree is mutated. Live Photo components never
become separate Apple uploads. Reports and original-path receipts survive moves.
"""
import argparse
from collections import Counter,defaultdict
import json
import os
from pathlib import Path
import subprocess
import time

import photo_settings as settings
import photo_pass
import review_queue as queue
import merge_photos as media
import organize_dates as dates
import nsfw_inputs as external
import nsfw_review
from live_photo_components import component_name,copy_hold,approved_still,standalone_plan,plan_group,stills_only_plan,import_component_plan
from live_photo_cleanup import native_assets
from photo_names import choose
from privacy_storage import copy_verified,verify
from sync_local_photo_folders import atomic_copy,INTAKE
from two_library_duplicates import read_scan,write_json
from onedrive_verify import guard_store
from windows_process_check import ensure_digikam_closed


def scan_folder(config,folder,out,workers):
    scoped={**config,'roots':{**config['roots'],'onedrive':folder}}
    summary=photo_pass.run({**scoped,'require_media_metadata':True},{'junk','nsfw'},['onedrive'],out/'scan',timeout=300,workers=workers)
    rows=read_scan(out/'scan'/'files.jsonl')
    for row in rows:row['root']=str(config['roots']['onedrive'])
    write_json(out/'intake.json',{'folder':str(folder),'summary':summary,'rows':rows})
    return rows


def target_index(config,folder,rows,out):
    """One metadata crawl per destination; hash only source-sized candidates."""
    saved=out/'destination-comparison.json'
    if saved.exists():
        data=json.loads(saved.read_text(encoding='utf-8'))
        if data.get('issues'):raise ValueError('Saved destination inventory has unresolved issues')
        matches=defaultdict(list)
        for item in data['matches']:
            for name in item['paths']:
                path=Path(name);media.inside(path,config['roots'][item['gallery']])
                if not config.get('_current_import_index'):verify(path,item['sha256'],item['bytes'])
                matches[(item['gallery'],item['sha256'],item['bytes'])].append(name)
        return matches,defaultdict(set,{label:set(values) for label,values in data['unavailable_sizes'].items()})
    sizes={r['bytes'] for r in rows if r.get('sha256')};keys={(r.get('sha256'),r['bytes']) for r in rows}
    roots={k:config['roots'][k] for k in (('icloud',) if config.get('import_mode') else ('onedrive','icloud'))}
    mappings=native_assets(config['icloud_database']) if config.get('icloud_database') else {}
    inventory,issues=photo_pass.inventory(roots,mappings,excluded={folder})
    if config.get('import_live_photos')=='stills_only':config['_import_gallery_candidates']=inventory
    from hash_index import HashIndex
    index_path=Path(config.get('hash_index',Path(config['review_state']).parent/'hash-index.sqlite'))
    for root in [folder,*config['roots'].values()]:
        if index_path.resolve().is_relative_to(Path(root).resolve()):
            raise ValueError('Hash index must stay outside media roots')
    cache=HashIndex(index_path)
    if not issues:
        for label,root in roots.items():
            cache.prune(root,[r['path'] for r in inventory if r['label']==label])
    matches=defaultdict(list);unavailable=defaultdict(set);checks=0
    candidates=[r for r in inventory if r['bytes'] in sizes and not r['status'].startswith('excluded_')]
    reader=queue.FingerprintWorker(45);started=time.monotonic();last_progress=0;processed=0
    def progress(status='running',force=False):
        nonlocal last_progress
        if not force and time.monotonic()-last_progress<2:return
        photo_pass.write_progress(out/'comparison-progress.json',{
            'status':status,'inventoried':len(inventory),'candidates':len(candidates),
            'processed':processed,'remaining':len(candidates)-processed,
            'matched_paths':sum(len(v) for v in matches.values()),
            'unavailable_sizes':sum(len(v) for v in unavailable.values()),
            'elapsed_seconds':round(time.monotonic()-started,2),'hash_index':cache.stats()})
        last_progress=time.monotonic()
    progress(force=True)
    try:
        for row in candidates:
            if config.get('import_mode'):
                from scan_lifecycle import check_stop
                check_stop(out)
            label=row['label'];path=Path(row['path'])
            if row['placeholder']:
                cache.forget(roots[label],path);unavailable[label].add(row['bytes'])
            else:
                try:
                    actual=cache.read(roots[label],path,reader);checks+=1
                    identity=(actual['sha256'],actual['bytes'])
                    if identity in keys:matches[(label,*identity)].append(row['path'])
                except (OSError,ValueError,subprocess.TimeoutExpired):unavailable[label].add(row['bytes'])
            processed+=1;progress()
    finally:
        reader.close();cache.close()
        progress('comparison_complete' if processed==len(candidates) else 'interrupted',force=True)
    write_json(out/'destination-comparison.json',{'inventoried':len(inventory),'candidate_hashes':checks,
        'hash_index':cache.stats(),
        'issues':issues,'unavailable_sizes':{k:sorted(v) for k,v in unavailable.items()},
        'matches':[{'gallery':k[0],'sha256':k[1],'bytes':k[2],'paths':v} for k,v in matches.items()]})
    if issues:raise ValueError('Destination inventory incomplete; inspect destination-comparison.json')
    return matches,unavailable


def groups(rows,config=None):
    """Join same-stem and embedded-id peers before applying either filter."""
    parents=list(range(len(rows)))
    def find(i):
        while parents[i]!=i:parents[i]=parents[parents[i]];i=parents[i]
        return i
    seen={}
    for i,row in enumerate(rows):
        p=Path(row['path'])
        named=p.with_suffix('') if p.suffix.lower() in media.SIDECARS else p
        keys=[('name',str(p.parent).casefold(),component_name(named)[0])]
        # Identical still bytes do not make a native Live Photo and a standalone
        # photo the same asset. Preserve association by name/embedded ID, but do
        # not let content alone merge these different asset types for review.
        if row.get('sha256'):
            native=bool(row.get('native_live_photo') or row.get('content_identifier'))
            keys.append(('content',row['sha256'],row['bytes'],native))
        if row.get('content_identifier'):keys.append(('apple_id',str(row['content_identifier']).strip().casefold()))
        for key in keys:
            if key in seen:parents[find(i)]=find(seen[key])
            else:seen[key]=i
    result=defaultdict(list)
    for i,row in enumerate(rows):result[find(i)].append(row)
    associated=list(result.values())
    # Imported media components are independent under the corrected policy.
    # Retain association groups containing edits/sidecars or unknown file types.
    independent=(config or {}).get('import_mode') and (config or {}).get('import_live_photos')=='preserve_components'
    complete=(config or {}).get('_import_inventory_complete') or (config or {}).get('_inventory_complete')
    if independent and complete:
        return [part for group in associated for part in
            ([[row] for row in group] if all(row.get('library')=='import' and
                row.get('kind') in {'photo','video'} for row in group) else [group])]
    return associated


def classify(group,state,imported,wanted,deleted,private=(),checks=('nsfw','junk'),review_holds=False):
    for row in group:
        h=row.get('sha256')
        if not h:return 'held','Source bytes unavailable'
        prior=state['items'].get(h) or imported['items'].get(h)
        if h not in wanted and prior and prior.get('nsfw_confirmation'):
            return 'private','Previously confirmed private content; never reimport to ordinary galleries'
    if any(r.get('sha256') in private for r in group):
        if any(r.get('sha256') in wanted for r in group):return 'held','Keep-protected companion conflicts with privacy route'
        return 'nsfw','Known private identity; move recurring copies into NSFW review'
    for row in group:
        h=row['sha256'];prior=state['items'].get(h) or imported['items'].get(h)
        if h not in wanted and prior and prior['status'] in {'reviewing','copy_pending'}:
            # A past suspicion cannot bypass today's detector, but fresh
            # evidence-supported NSFW still routes a returned exposed copy.
            if any(r.get('checks',{}).get('nsfw',{}).get('candidate') for r in group):
                if any(r.get('sha256') in wanted for r in group):return 'held','Keep-protected bundle member conflicts with filter; preserve complete bundle'
                return 'nsfw','Fresh sexual-content evidence; route returned copy into review'
            return 'held','Existing review decision; do not override or recreate handled review files'
        if h in deleted and h not in wanted:
            return ('import_decision' if review_holds else 'held'),'Past deletion exclusion without a private-content routing decision'
    # A broad/uncertain score is not a sexual-picture nomination. Keep unresolved
    # inputs pending rather than filling NSFW with nonsexual family photographs.
    for check in checks:
        if any(r.get('sha256') not in wanted and r['checks'].get(check,{}).get('candidate') for r in group):
            if any(r.get('sha256') in wanted for r in group):return 'held','Keep-protected bundle member conflicts with filter; preserve complete bundle'
            return check,'Local model nomination; human review required'
    for row in group:
        if row['sha256'] in wanted:continue
        if any(row['checks'].get(c,{}).get('status') not in {'analyzed','not_applicable'} for c in checks):
            return 'held','One or more requested filters are incomplete'
    return 'accepted','No selected filter nomination; not a permanent keep decision'


def review_group(config,group,category,out,backup_root,journal,recycler,selection_file=None,write_index=True):
    review=config['nsfw_review_folder'] if category=='nsfw' else config['review_folder']
    script_reason=category
    if category!='nsfw':
        from review_categories import destination
        review,script_reason=destination(config,group,category)
    review.mkdir(parents=True,exist_ok=True)
    normalized=[]
    for row in group:
        check=row['checks'].get(category,{})
        normalized.append({**row,**check,'status':'analyzed','candidate':True,'analysis_check':category if category in {'nsfw','quality','tiktok'} else 'junk',
            'rule':check.get('rule','unified-pass-v1-bundle-intake'),'category':category,
            'reason':'Linked bundle/filter intake; human review required'})
    selections=selection_file or out/('review-'+group[0]['sha256']+'.jsonl')
    with selections.open('w',encoding='utf-8') as stream:
        for row in normalized:stream.write(json.dumps(row)+'\n')
    with queue.locked(config['review_state']):
        stage=config['review_state'].parent/'.staging';stage.mkdir(exist_ok=True)
        result=queue.collect(selections,config['roots'],config['review_state'],review,stage,journal,
            settings.hashes(config['safe_ledgers']),lambda:settings.hashes(config['safe_ledgers']),
            inventory_rows=group,eligible_hashes={r['sha256'] for r in group})
        if result['held']:raise ValueError('Review delivery held: '+json.dumps(result['held']))
        state=queue.load_state(config['review_state'])
        prepared=[]
        for row in group:
            h=row['sha256'];size=row['bytes'];source=Path(row['path']);item=state['items'][h]
            if item.get('nsfw_confirmation'):raise ValueError('Confirmed content requires the privacy delivery workflow')
            # A returned copy must not leak just because the old review copy was handled.
            target=Path(item['review_path'])
            if target.parent.resolve()!=review.resolve():target=choose(review,source.name,h,size)
            media.inside(target,review);copy_verified(source,target,review,h,size);verify(target,h,size)
            found=queue.origin(source,row['label'],config['roots'][row['label']])
            if not any(r['path']==str(source) for r in item['origins']):item['origins'].append(found)
            item.update(review_path=str(target),status='reviewing',script_reason=script_reason)
            item.pop('review_copy_handled_by_user',None)
            if not any(e.get('category')==category for e in item.get('evidence',[])):
                item.setdefault('evidence',[]).append({'category':category,'analysis_check':category,'reason':'Recurring private copy routing'})
            queue.save_state(config['review_state'],state)
            backup=None
            prepared.append((row,target,backup))
        # Complete companion preservation precedes the first source removal.
        for row,target,backup in prepared:
            h=row['sha256'];source=Path(row['path']);item=state['items'][h]
            if h in settings.hashes(config['safe_ledgers']):raise ValueError('New keep decision blocks review move')
            ensure_digikam_closed();verify(source,h,row['bytes']);verify(target,h,row['bytes'])
            receipt={'source':str(source),'destination':str(target),'backup':None,'sha256':h,'bytes':row['bytes'],'category':category,'script_reason':script_reason}
            item.setdefault('folder_ingest_moves',{})[str(source)]={**receipt,'status':'move_intent'}
            queue.save_state(config['review_state'],state);media.journal(journal,{'action':'folder_review_move_intent',**receipt})
            recycler(str(source))
            if source.exists():raise ValueError('Review source removal incomplete')
            item['folder_ingest_moves'][str(source)]['status']='moved_verified'
            queue.save_state(config['review_state'],state);media.journal(journal,{'action':'folder_review_moved_verified',**receipt})
        if write_index:queue.write_index(config['review_state'],state)
    return len(prepared)


def retire_import_duplicates(config,rows,out,recycler=None):
    """Retire exact standalone source extras before inference; never split bundles."""
    if recycler is None:
        from delete_icloud_clutter import modern_recycler
        recycler=modern_recycler()
    journal=out/'actions.jsonl'
    for group in groups(rows):
        identities={(r.get('sha256'),r['bytes'],r['kind']) for r in group}
        if len(group)<2 or len(identities)!=1:continue
        if any(not r.get('sha256') or r.get('status')!='verified_local_bytes' or
               r['kind'] not in {'photo','video'} or r.get('content_identifier') or
               r.get('native_live_photo') or r.get('import_live_motion') or
               r.get('_prepared_metadata',r.get('metadata',{})).get('status')!='cataloged'
               for r in group):continue
        keeper,*extras=sorted(group,key=lambda r:r['path'].casefold())
        for row in extras:
            source=Path(row['path']);target=Path(keeper['path'])
            event={'source':str(source),'keeper':str(target),'sha256':row['sha256'],'bytes':row['bytes']}
            try:
                from scan_lifecycle import check_stop
                check_stop(out)
                media.inside(source,config['import_folder']);media.inside(target,config['import_folder'])
                ensure_digikam_closed()
                verify(source,row['sha256'],row['bytes']);verify(target,row['sha256'],row['bytes'])
                media.journal(journal,{'action':'import_duplicate_retirement_intent',**event})
                recycler(str(source))
                if source.exists():raise ValueError('Duplicate source still exists after recycling')
                verify(target,row['sha256'],row['bytes'])
                media.journal(journal,{'action':'import_duplicate_retired_verified',**event})
                row['import_duplicate_keeper']=str(target)
            except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
                row['import_duplicate_hold']=str(exc)
    return rows


def resume_completed(rows,journal,config=None):
    """Freshly verify durable completed moves; absent files alone prove nothing."""
    completed={}
    if journal.exists():
        from expand_reference_selections import read_jsonl
        for event in read_jsonl(journal):
            if event.get('action') in {'import_duplicate_retired_verified','folder_review_moved_verified','folder_existing_privacy_routed_verified','folder_source_retired_verified','folder_live_motion_discarded_verified'}:
                completed[(event['source'],event['sha256'],event['bytes'])]=event
    pending=[];verified=[];held=[];native_checked=set()
    for row in rows:
        event=completed.get((row['path'],row.get('sha256'),row['bytes']))
        if event is None:pending.append(row);continue
        try:
            if Path(row['path']).exists():raise ValueError('Completed source returned; use a new intake to route the new copy')
            if event['action']=='import_duplicate_retired_verified':
                keeper=Path(event['keeper'])
                if config is None:raise ValueError('Duplicate receipt requires import configuration')
                media.inside(keeper,config['import_folder'])
                if keeper.exists():verify(keeper,row['sha256'],row['bytes'])
                else:
                    # The sole keeper may have since moved to iCloud or review.
                    retained=completed.get((str(keeper),row['sha256'],row['bytes']))
                    destinations=[retained[k] for k in ('destination','icloud','review_path') if retained and retained.get(k)]
                    if not destinations:raise ValueError('Duplicate keeper has no completed delivery receipt')
                    for destination in destinations:verify(Path(destination),row['sha256'],row['bytes'])
                verified.append(event);continue
            if event['action']=='folder_live_motion_discarded_verified':
                from import_live_stills import verify_keeper
                if event.get('policy')!='stills_only':raise ValueError('Unknown motion discard policy')
                verify_keeper(event['still_keeper']);verified.append(event);continue
            destinations=[event[k] for k in ('destination','onedrive','icloud','review_path') if event.get(k)]
            if not destinations:raise ValueError('Completed receipt has no retained destination')
            for path in destinations:verify(Path(path),row['sha256'],row['bytes'])
            if event.get('icloud_conversion'):
                converted=event['icloud_conversion']
                if config is None:raise ValueError('Conversion receipt requires destination configuration')
                media.inside(Path(converted['path']),config['roots']['icloud'])
                verify(Path(converted['path']),converted['sha256'],converted['bytes'])
            if event.get('icloud_native'):
                if config is None:raise ValueError('Native cloud receipt requires a current provider check')
                from live_photo_cloud import refresh_receipt
                receipt=event['icloud_native'];key=receipt['pair_key']
                if not any(r.get('sha256')==row['sha256'] and r.get('bytes')==row['bytes'] for r in receipt.get('resources',{}).values()):
                    raise ValueError('Native receipt does not cover this completed source')
                if key not in native_checked:refresh_receipt(config,receipt);native_checked.add(key)
            verified.append(event)
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
            held.append({'sources':[row['path']],'error':'Completed receipt needs attention: '+str(exc)})
    return pending,verified,held


def confirmed_again(config,group,backup_root,journal,recycler):
    """Reuse an existing human approval; never approve a model nomination."""
    from photo_names import original_name
    state=queue.load_state(config['review_state']);imported=external.load(config)
    prepared=[]
    for row in group:
        h=row['sha256'];size=row['bytes'];source=Path(row['path'])
        item=state['items'].get(h) or imported['items'].get(h)
        if not item or item['status']!='reviewing' or not item.get('nsfw_confirmation') or h in settings.hashes(config['safe_ledgers']):
            raise ValueError('Existing human privacy approval required')
        target=choose(config['nsfw_confirmed_folder'],original_name(item),h,size)
        backup=None
        copy_verified(source,target,config['nsfw_confirmed_folder'],h,size)
        prepared.append((row,target,backup))
    intake=nsfw_review.process(config,apply=True)
    if intake['held']:raise ValueError('Existing privacy intake held: '+json.dumps(intake['held']))
    for row,target,backup in prepared:
        source=Path(row['path']);h=row['sha256'];size=row['bytes']
        if h in settings.hashes(config['safe_ledgers']):raise ValueError('New keep blocks privacy routing')
        ensure_digikam_closed();verify(source,h,size);verify(target,h,size)
        receipt={'source':str(source),'destination':str(target),'backup':None,'sha256':h,'bytes':size,'approval':'existing_human_hash_confirmation'}
        media.journal(journal,{'action':'folder_existing_privacy_route_intent',**receipt})
        recycler(str(source))
        if source.exists():raise ValueError('Privacy intake source still present')
        media.journal(journal,{'action':'folder_existing_privacy_routed_verified',**receipt})
    return len(prepared)


def finish(config,folder,out,rows,apply=False,recycler=None,target_matches=None):
    from privacy_routes import hashes as private_hashes
    strict_import=config.get('import_mode',False)
    import import_live_stills as live_stills
    live_stills.prepare(config,rows,config.get('_import_inventory_complete',False))
    wanted,deleted=settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
    state=queue.load_state(config['review_state']);imported=external.load(config)
    result={'scope':str(folder),'apply':apply,'files':len(rows),'actions':{},'items':[],'held':[],
        'cloud_sync_verified':False,'permanent_deletions':0,'model_passes':1}
    if live_stills.enabled(config):result['gallery_still_match_errors']=config.get('_live_still_match_errors',[])
    matches,unavailable=target_matches if target_matches is not None else target_index(config,folder,rows,out)
    if strict_import:recover_local_matches(config,out/'actions.jsonl',rows,matches)
    counts=Counter();journal=out/'actions.jsonl';backup_root=config['preservation']/'folder-intake'/out.name
    prior_imports=_import_intents(config,journal) if strict_import else set()
    pending,completed,resume_holds=resume_completed(rows,journal,config)
    motions=[r for r in pending if live_stills.enabled(config) and live_stills.motion_plan(r)]
    motion_paths={r['path'] for r in motions}
    pending=[r for r in pending if r['path'] not in motion_paths]
    if apply:live_stills.record_plans(config,out,motions)
    from live_photo_cloud import read_receipts, receipt_for_group, finish_native_group
    native_receipts=read_receipts(config) if strict_import else {}
    result['resumed_verified']=len(completed);result['completed_receipts']=completed;result['held'].extend(resume_holds)
    for event in completed:
        action=event['action']
        if action=='import_duplicate_retired_verified':counts['source_duplicates_recycled']+=1
        elif action=='folder_live_motion_discarded_verified':counts['live_motion_recycled']+=1
        elif action=='folder_review_moved_verified':counts['moved_'+event['category']]+=1
        elif action=='folder_existing_privacy_routed_verified':counts['already_confirmed_to_privacy_flow']+=1
        else:
            if not strict_import:counts['duplicates_to_review' if event.get('review_path') else 'organized_onedrive']+=1
            if strict_import:counts['imports_completed']+=1
            if event.get('icloud') or event.get('icloud_native') or event.get('icloud_conversion'):counts['apple_verified']+=1
            if event.get('icloud_native'):counts['native_cloud_resources_verified']+=1
            if event.get('icloud_conversion'):counts['apple_converted']+=1
    private=private_hashes(config)
    stage=out/'.staging';stage.mkdir(exist_ok=True)
    if apply:
        ensure_digikam_closed()
        if recycler is None:
            from delete_icloud_clutter import modern_recycler
            recycler=modern_recycler()
    pending_groups=groups(pending,config);last_report=0;stopped=False
    result['groups_total']=len(pending_groups)
    for group_number,group in enumerate(pending_groups):
        if time.monotonic()-last_report>=15:
            result.update(actions=dict(counts),groups_processed=group_number,status='import_running')
            write_json(out/'report.json',result);last_report=time.monotonic()
            print(json.dumps({'stage':'import_delivery','groups_processed':group_number,
                'groups_total':len(pending_groups),'actions':dict(counts),'holds':len(result['held'])}),flush=True)
        if (out/'stop.request').exists():
            stopped=True;break
        missing=[r for r in group if not Path(r['path']).exists()]
        for row in missing:result['held'].append({'sources':[row['path']],'error':'Input absent without a completed move receipt; no decision inferred'})
        group=[r for r in group if Path(r['path']).exists()]
        if not group:continue
        if strict_import:plan_group(group,config.get('_import_inventory_complete',False))
        disposition,reason=classify(group,state,imported,wanted,deleted,private,
            ('nsfw','junk','tiktok','quality') if strict_import else ('nsfw','junk'),
            review_holds=strict_import and config.get('import_review_holds',False))
        entry={'sources':[r['path'] for r in group],'disposition':disposition,'reason':reason,'destinations':[]}
        result['items'].append(entry)
        try:
            if disposition=='held':raise ValueError(reason)
            if not apply:counts['planned_'+disposition]+=len(group);continue
            if disposition=='private':
                from privacy_routes import route
                routed=route(config,group,out,True,recycler)
                counts['already_confirmed_to_privacy_flow']+=routed['confirmed_routed_files']
                counts['moved_nsfw']+=routed['moved_files']
                result['held'].extend(routed['held']);continue
            if strict_import and disposition in {'junk','tiktok','quality','import_decision'}:
                counts['moved_'+disposition]+=review_group(config,group,disposition,out,backup_root,journal,recycler);continue
            if disposition=='junk':
                counts['junk_reported_left_in_place']+=len(group)
                result['held'].append({'sources':entry['sources'],'error':'Junk nomination reported; left in place for a decision'})
                continue
            if disposition=='nsfw':
                counts['moved_'+disposition]+=review_group(config,group,disposition,out,backup_root,journal,recycler);continue
            # Preserve every member before sorting any member of a bundle.
            prepared=[];bundled=len(group)>1 and len({r['kind'] for r in group})>1
            bundled |= any(r.get('content_identifier') or r.get('native_live_photo') for r in group)
            native_receipt=receipt_for_group(group,native_receipts) if strict_import else None
            # A partially retired bundle still belongs to its verified native
            # asset, even when its remaining source looks like a standalone still.
            if native_receipt is not None:bundled=True
            if strict_import and config.get('import_live_photos')=='preserve_components' and all(import_component_plan(r) for r in group):
                bundled=False;native_receipt=None;entry['asset_plan']='preserve_supplied_media_components'
            if live_stills.enabled(config) and all(stills_only_plan(r) for r in group):
                bundled=False;native_receipt=None;entry['asset_plan']='import_still_only'
            if strict_import and native_receipt is None and all(standalone_plan(r) for r in group):
                bundled=False;entry['asset_plan']='standalone_photo_no_supplied_companion'
            if strict_import and native_receipt is None and all(approved_still(r,group) for r in group):
                bundled=False;entry['user_approved_still']=True
            # Reusing every existing byte-identical component changes no gallery
            # asset. Verify/preserve the whole group before retiring any input.
            reuse_only=strict_import and all(matches.get((label,r['sha256'],r['bytes']))
                for r in group for label in (('icloud',) if strict_import else ('onedrive','icloud')))
            if strict_import:
                if bundled:
                    raise ValueError('Native paired import is unavailable in the iCloud-only importer; source bundle retained')
                if not reuse_only and any(r['kind']=='sidecar' for r in group):raise ValueError('Edit/sidecar application unsupported; complete source bundle retained')
                for row in group:
                    if row['sha256'] not in wanted and (row.get('metadata',{}).get('status')!='cataloged' or row.get('metadata_hold')):
                        raise ValueError('Association metadata incomplete; IMPORT original retained')
                    if not reuse_only and copy_hold({**row,'target':'icloud'},group):raise ValueError('Apple asset import plan required; IMPORT original retained')
            bundle_date=next((dates.choose_date(Path(r['path']),Path(Path(r['path']).name),'mtime')[0] for r in group if r['kind']=='photo'),None)
            for row in group:
                source=Path(row['path']);media.inside(source,folder);h=row['sha256'];size=row['bytes'];verify(source,h,size)
                backup=None
                stamp,date_source,_=dates.choose_date(source,Path(source.name),'mtime')
                stamp=bundle_date or stamp
                if stamp is None:raise ValueError('No organization date')
                target=None;existing=[]
                if not strict_import:
                    target_folder=config['roots']['onedrive']/stamp.strftime('%Y/%m')
                    existing=[Path(p) for p in matches.get(('onedrive',h,size),[]) if strict_import or dates.date_sorted(Path(p).relative_to(config['roots']['onedrive']))]
                    if strict_import and not existing and size in unavailable['onedrive']:
                        raise ValueError('Same-sized OneDrive bytes unavailable; IMPORT original retained')
                    target=sorted(existing,key=lambda p:str(p).casefold())[0] if existing else choose(target_folder,source.name,h,size)
                    if strict_import and not existing:
                        _guard_import_intents(prior_imports,row,'onedrive')
                        atomic_copy({**row,'target':'onedrive'},target,config['roots']['onedrive'],stage,journal,config.get('import_ledger'),peers=group)
                        prior_imports.add(('onedrive',h,size))
                    else:copy_verified(source,target,config['roots']['onedrive'],h,size)
                    if not existing:matches[('onedrive',h,size)].append(str(target))
                cloud_matches=matches.get(('icloud',h,size),[]);cloud=None;cloud_hold=None
                if cloud_matches:cloud=Path(sorted(cloud_matches)[0]);verify(cloud,h,size)
                elif bundled or copy_hold(row,group):cloud_hold='Complete Live Photo asset import required; separate still/motion uploads blocked'
                elif size in unavailable['icloud']:cloud_hold='Same-sized iCloud bytes unavailable; download before proving missing copy'
                elif source.suffix.lower() not in INTAKE:cloud_hold='Format requires an Apple import plan'
                else:
                    if strict_import:_guard_import_intents(prior_imports,row,'icloud')
                    cloud=choose(config['roots']['icloud'],source.name,h,size)
                    atomic_copy({**row,'target':'icloud'},cloud,config['roots']['icloud'],stage,journal,config['delivery_ledger'],peers=group)
                    if strict_import:prior_imports.add(('icloud',h,size))
                    matches[('icloud',h,size)].append(str(cloud));counts['apple_copied']+=1
                if strict_import:
                    target=cloud;existing=cloud_matches
                entry['destinations'].append({'source':str(source),'onedrive':str(target) if not strict_import else None,'icloud':str(cloud) if cloud else None,
                    'date_source':date_source,'cloud_hold':cloud_hold})
                prepared.append((row,target,cloud,backup,cloud_hold,bool(existing)))
            if strict_import and any(not cloud or cloud_hold for _,_,cloud,_,cloud_hold,_ in prepared):
                raise ValueError('iCloud destination must verify before clearing IMPORT: '+str([p[4] for p in prepared if p[4]]))
            for row,target,cloud,backup,cloud_hold,duplicate in prepared:
                source=Path(row['path']);h=row['sha256'];size=row['bytes']
                if h in settings.review_move_hashes():raise ValueError('New pending review decision blocks intake retirement')
                ensure_digikam_closed();verify(source,h,size);verify(target,h,size)
                if cloud:verify(cloud,h,size)
                receipt={'source':str(source),'onedrive':str(target) if not strict_import else None,'icloud':str(cloud) if cloud else None,'backup':None,'sha256':h,'bytes':size}
                # Extra input copies go to DUPES after keeper verification.
                if duplicate and not strict_import:
                    dest=choose(config['review_folder']/'DUPES',source.name,h,size,reuse=False)
                    copy_verified(source,dest,config['review_folder'],h,size)
                    receipt['review_path']=str(dest)
                media.journal(journal,{'action':'folder_source_retirement_intent',**receipt})
                recycler(str(source))
                if source.exists():raise ValueError('Intake source still exists after recoverable removal')
                verify(target,h,size)
                media.journal(journal,{'action':'folder_source_retired_verified',**receipt})
                if strict_import:
                    counts['imports_completed']+=1
                    if duplicate:counts['reused_icloud_copies']+=1
                else:counts['duplicates_to_review' if duplicate else 'organized_onedrive']+=1
                if cloud:counts['apple_verified']+=1
                if cloud_hold:result['held'].append({'path':str(target),'error':cloud_hold})
        except (OSError,ValueError,RuntimeError,KeyError,subprocess.TimeoutExpired) as exc:
            result['held'].append({'sources':entry['sources'],'error':str(exc)})
    if not stopped:
        live_stills.finish(config,out,motions,apply,recycler,result,counts)
        stopped=result.get('status')=='stopped'
    result['groups_processed']=group_number if stopped and pending_groups else len(pending_groups)
    result['actions']=dict(counts);result['status']='pending' if result['held'] else 'local_processing_verified_cloud_pending' if apply else 'plan_ready'
    if apply and not stopped and any(e['disposition']=='private' for e in result['items']):
        result['inboxes']=nsfw_review.service(config)
        result['held'].extend(result['inboxes']['held'])
        if result['held']:result['status']='pending'
    if stopped:result['status']='stopped'
    write_json(out/'report.json',result)
    (out/'report.md').write_text(('# IMPORT processing\n\n' if strict_import else '# Folder intake\n\n')+f"Files: {len(rows)}. Actions: {dict(counts)}. Holds: {len(result['held'])}.\n\n"+
        'Only intake originals were moved. Originals and destination bytes are freshly verified; no backups are retained. Cloud sync remains unverified.\n\n'+
        ('Successful imports have verified bytes in the local iCloud gallery before recoverable input removal. The iPhone supplies OneDrive copies. Failed/incomplete imports remain in IMPORT. NSFW uses NSFW; junk/TikTok/quality use FOR_REVIEW.\n\n' if strict_import else '')+
        ('Imported Live Photos keep the verified still only. Matched motion is recycled after still delivery; unmatched motion goes to FOR_REVIEW/Live Photos.\n\n' if live_stills.enabled(config) else
         'NSFW and junk nominations await your review; no automatic human confirmations. Native Live Photos require both exact originals on one cloud asset.\n\n')+
        '\n'.join('- '+json.dumps(hold) for hold in result['held']),encoding='utf-8')
    return result


def _import_intents(config,journal):
    """Index durable delivery intent once for the complete intake batch."""
    from sync_local_photo_folders import read_lines
    paths={journal}|{config[k] for k in ('delivery_ledger','import_ledger') if config.get(k)}
    return {(e.get('target','icloud'),e['sha256'],e['bytes']) for path in paths for e in read_lines(path)
            if e.get('action') in {'copy_intent','copy_verified','upload_intent','queued_local_verified'}
            and e.get('sha256') and e.get('bytes') is not None}


def recover_local_matches(config,journal,rows,matches):
    """A prior pass may have published after the saved destination inventory."""
    from sync_local_photo_folders import read_lines
    needed={(r.get('sha256'),r['bytes']) for r in rows}
    paths={journal}|{config[k] for k in ('delivery_ledger','import_ledger') if config.get(k)}
    checked=set()
    for path in paths:
        for event in read_lines(path):
            label=event.get('target','icloud');identity=(event.get('sha256'),event.get('bytes'))
            dest=event.get('destination')
            if label not in ('onedrive','icloud') or identity not in needed or not dest:continue
            key=(label,*identity)
            if dest in matches[key] or (key,dest) in checked:continue
            checked.add((key,dest))
            try:
                media.inside(Path(dest),config['roots'][label]);verify(Path(dest),*identity)
            except (OSError,ValueError,subprocess.TimeoutExpired):continue
            matches[key].append(dest)


def _guard_import_intents(prior,row,target):
    """An earlier uncertain publication needs current matching bytes, not retry."""
    if (target,row['sha256'],row['bytes']) in prior:
        raise ValueError('Previous import intent lacks a current verified match; no blind retry')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--reuse',action='store_true')
    parser.add_argument('--workers',type=int,default=0,help='0 chooses a CPU/RAM-aware worker count')
    parser.add_argument('--route-private-sha256',action='append',default=[],help='Explicitly register a known private input for recurring NSFW routing; requires --apply')
    args=parser.parse_args();config=settings.load();folder=args.folder.resolve();out=args.output.resolve()
    media.inside(folder,config['roots']['onedrive'])
    if folder==config['roots']['onedrive']:raise ValueError('Select an intake subtree, not the whole Camera Roll')
    guard_store(out,[],list(config['roots'].values()));out.mkdir(parents=True,exist_ok=True)
    with queue.locked(out/'intake-state.json'):
        if args.reuse:
            saved=json.loads((out/'intake.json').read_text(encoding='utf-8'))
            if Path(saved['folder'])!=folder:raise ValueError('Saved intake folder differs')
            rows=saved['rows']
        else:
            if (out/'intake.json').exists():raise ValueError('Use --reuse for a saved intake')
            rows=scan_folder(config,folder,out,args.workers)
        if args.route_private_sha256:
            if not args.apply:raise ValueError('Private routing registration requires --apply')
            from privacy_routes import register
            register(config,rows,args.route_private_sha256)
        result=finish(config,folder,out,rows,args.apply)
    print(json.dumps({'status':result['status'],'files':result['files'],'actions':result['actions'],'holds':len(result['held']),'report':str(out/'report.md')}))
    return 2 if result['held'] else 0


if __name__=='__main__':raise SystemExit(main())
