"""CONFIRMED approves NSFW; RESTORE keeps it. --deliver runs both destinations."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import re

import photo_settings as settings
import review_queue as queue
import scan_reference_photos as scan
import merge_photos as media
import nsfw_inputs as external
from onedrive_verify import guard_store
from two_library_duplicates import write_json
from windows_process_check import ensure_digikam_closed


def reject(config,digests,apply=False):
    """User-declared false positives: exact-path restore, then durable exclusion.

    Only these hashes are processed; no gallery crawl, detector or cloud action.
    """
    from privacy_storage import copy_verified,verify
    digests=set(digests)
    if not digests or any(not re.fullmatch('[0-9a-f]{64}',h) for h in digests):raise ValueError('Exact SHA-256 values required')
    settings.hashes(config['safe_ledgers'])
    queue.layout(settings.review_roots(config),config['review_state'],config['nsfw_review_folder'],config['restore_folder'])
    result={'requested_hashes':sorted(digests),'apply':apply,'restored_keeps':0,'removed_nsfw_review_files':0,
        'held':[],'gallery_crawls':0,'model_runs':0,'cloud_actions':0}
    if not apply:return result
    ensure_digikam_closed()
    with queue.locked(config['review_state']):
        state=queue.load_state(config['review_state']);rows=[];old_copies={}
        stage=config['review_state'].parent/'.staging';stage.mkdir(exist_ok=True)
        restore=config['restore_folder'];restore.mkdir(parents=True,exist_ok=True)
        journal=config['review_state'].parent/'review-actions.jsonl'
        for digest in sorted(digests):
            try:
                item=state['items'].get(digest)
                if not item or not any(e.get('category')=='nsfw' or e.get('analysis_check')=='nsfw' for e in item.get('evidence',[])):
                    raise ValueError('Recorded NSFW review provenance required')
                size=item['representative']['bytes']
                item['nsfw_user_correction']={'decision':'false_positive','method':'explicit_user_hash_correction','utc':media.utc()}
                queue.save_state(config['review_state'],state)
                # The human correction is authoritative even when restore bytes
                # are unavailable; protection does not claim successful restoration.
                for ledger in config['safe_ledgers']:
                    data=json.loads(ledger.read_text(encoding='utf-8-sig'))
                    data['excluded_sha256']=sorted(set(data['excluded_sha256'])|{digest})
                    write_json(ledger,data)
                # Keep destinations exact; never invent locations from a filename.
                sources=[Path(item['review_path'])]+[Path(r['path']) for r in item['origins']]
                sources += [Path(r['backup']) for r in item.get('nsfw_review_moves',{}).values() if r.get('status')=='moved_verified']
                source=next((p for p in sources if p.exists()),None)
                if source is None:raise ValueError('Recorded review/original/backup bytes unavailable')
                verify(source,digest,size)
                from photo_names import choose,original_name
                target=choose(restore,original_name(item),digest,size)
                media.journal(journal,{'action':'nsfw_false_positive_restore_intent','sha256':digest,'source':str(source),'restore':str(target),'utc':media.utc()})
                copy_verified(source,target,restore,digest,size)
                rows.append({'path':str(target)});old_copies[digest]=Path(item['review_path'])
            except (OSError,ValueError,subprocess.TimeoutExpired) as exc:result['held'].append({'sha256':digest,'error':str(exc)})
        restored=queue.restore_files(settings.review_roots(config),config['review_state'],restore,stage,journal,inventory_rows=rows,eligible_hashes=digests)
        result['restored_keeps']=restored['new_kept_hashes'];result['held'].extend(restored['held'])
        state=queue.load_state(config['review_state'])
        for digest,path in old_copies.items():
            if state['items'][digest]['status']!='kept':continue
            try:
                size=state['items'][digest]['representative']['bytes']
                kept=state['items'][digest]
                for destination in kept.get('restore_destinations',[kept['restore_destination']['path']]):
                    verify(Path(destination),digest,size)
                # Preserve RESTORE input; remove only the exact obsolete NSFW artifact.
                if path.exists():
                    media.inside(path,config['nsfw_review_folder']);verify(path,digest,size)
                    path.unlink();result['removed_nsfw_review_files']+=1
                media.journal(journal,{'action':'nsfw_false_positive_restored_and_excluded','sha256':digest,'utc':media.utc()})
            except (OSError,ValueError,subprocess.TimeoutExpired) as exc:result['held'].append({'sha256':digest,'error':str(exc)})
        queue.write_index(config['review_state'],state)
    out=config['output']/'nsfw-review';out.mkdir(parents=True,exist_ok=True)
    write_json(out/'false-positive-report.json',result)
    return result


def process(config,apply=False,setup=False):
    review=config['nsfw_review_folder'];restore=config['restore_folder']
    confirmed=config.get('nsfw_confirmed_folder',review/'CONFIRMED')
    if confirmed.parent!=review or confirmed.name.casefold()!='confirmed':
        raise ValueError('CONFIRMED must be the direct child of the NSFW review folder')
    queue.layout(settings.review_roots(config),config['review_state'],review,restore)
    media.inside(confirmed,review)
    wanted=settings.hashes(config['safe_ledgers']) | external.wanted(config)
    out=config['output']/'nsfw-review';guard_store(out,[],list(config['roots'].values()))
    out.mkdir(parents=True,exist_ok=True)
    if setup or apply:
        confirmed.mkdir(parents=True,exist_ok=True);restore.mkdir(parents=True,exist_ok=True)
    if setup and not apply:
        return {'setup':True,'confirmed_folder':str(confirmed),'restore_folder':str(restore),
            'gallery_crawls':0,'model_runs':0,'cloud_actions':0,'source_deletions':0,'held':[]}
    intake={};issues=[]
    for label,root in (('confirmed',confirmed),('restore',restore)):
        extensions=scan.PHOTOS|__import__('nsfw_analysis').VIDEOS|(set(media.SIDECARS) if label=='restore' else set())
        rows,errors=scan.inventory({label:root},extensions) if root.exists() else ([],[])
        intake[label]=rows;issues.extend(errors)
    result={'apply':apply,'confirmed_folder':str(confirmed),'restore_folder':str(restore),
        'confirmed_contents':[],'new_confirmations':0,'already_confirmed':0,'restore':{},'held':issues,
        'gallery_crawls':0,'model_runs':0,'cloud_actions':0,'source_deletions':0,
        'provider_status':'Run --deliver to request Private album and authenticated local Personal Vault; iPhone Hide remains manual'}
    # Read only intake files. RESTORE intent wins even if restoration is held.
    restore_hashes=set()
    for row in intake['restore']:
        try:restore_hashes.add(queue.fresh(Path(row['path']))['sha256'])
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
            result['held'].append({'path':row['path'],'error':str(exc)})
    with queue.locked(config['review_state']):
        state=queue.load_state(config['review_state'])
        imported=external.load(config);known_restore=[];external_kept=set()
        for row in intake['restore']:
            try:actual=queue.fresh(Path(row['path']));digest=actual['sha256']
            except (OSError,ValueError,subprocess.TimeoutExpired):continue  # Already reported above.
            if digest in imported['items'] and digest not in state['items']:
                if apply:
                    if imported['items'][digest]['status']!='kept':external_kept.add(digest)
                    item=imported['items'][digest]
                    item.update(status='kept',keep_path=row['path'],kept_utc=media.utc())
                    item['nsfw_privacy']={'status':'cancelled_by_restore'}
                    external.save(config,imported)
                    try:
                        ensure_digikam_closed()
                        from privacy_storage import copy_verified,verify
                        from photo_names import choose,original_name
                        stage=config['review_state'].parent/'.staging';stage.mkdir(exist_ok=True)
                        journal=config['review_state'].parent/'review-actions.jsonl'
                        source=queue.origin(Path(row['path']),'restore',restore)
                        index=queue.restore_gallery_index(config['roots']['onedrive'],{source['bytes']})
                        target=queue.deliver_restore_to_gallery(config['roots'],source,item,stage,journal,index)
                        item.update(keep_path=str(target),restore_backup=None,
                                    restore_destination={'path':str(target),'sha256':digest,'bytes':source['bytes'],'status':'local_verified_cloud_pending'})
                        external.save(config,imported)
                        verify(target,digest,source['bytes']);verify(Path(row['path']),digest,source['bytes'])
                        media.journal(journal,{'action':'restore_external_cleanup_intent','sha256':digest,'path':row['path'],'destination':str(target),'backup':None})
                        Path(row['path']).unlink()
                        media.journal(journal,{'action':'restore_external_cleared','sha256':digest,'path':row['path']})
                    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:result['held'].append({'path':row['path'],'error':str(exc)})
            else:known_restore.append(row)
        if apply and (known_restore or any(r['status']=='restore_pending' for r in state['items'].values())):
            ensure_digikam_closed()
            stage=config['review_state'].parent/'.staging';stage.mkdir(exist_ok=True)
            result['restore']=queue.restore_files(settings.review_roots(config),config['review_state'],restore,stage,
                config['review_state'].parent/'review-actions.jsonl',inventory_rows=known_restore)
            result['held'].extend(result['restore']['held']);state=queue.load_state(config['review_state'])
        else:result['restore']={'input_files':len(intake['restore']),'new_kept_hashes':0,'applied':False}
        result['restore']['external_kept_hashes']=len(external_kept)
        wanted |= settings.hashes(config['safe_ledgers']) | external.wanted(config)
        wanted |= {h for h,r in state['items'].items() if r['status'] in {'kept','restore_pending'}}
        seen=set()
        for row in intake['confirmed']:
            path=Path(row['path'])
            try:
                media.inside(path,confirmed);actual=queue.fresh(path);digest=actual['sha256']
                item=state['items'].get(digest)
                if digest in wanted or digest in restore_hashes:raise ValueError('RESTORE / reviewed-wanted decision takes precedence')
                if digest in seen:
                    if apply:
                        recorded=state['items'].get(digest) or imported['items'].get(digest)
                        if recorded and recorded.get('nsfw_confirmation'):
                            receipt=recorded['nsfw_confirmation']
                            receipt['paths']=sorted(set(receipt.get('paths',[receipt['path']]))|{str(path)})
                            if digest in state['items']:queue.save_state(config['review_state'],state)
                            else:external.save(config,imported)
                    continue
                if not item:
                    prior=imported['items'].get(digest)
                    entry={'sha256':digest,'bytes':actual['bytes'],'confirmed_copy':str(path),
                        'origin_kind':'external_confirmed','recorded_origins':[],
                        'apple_status':'reuse_exact_cloud_match_or_upload_new_original_then_Private',
                        'onedrive_status':'pending_authenticated_personal_vault_transfer'}
                    seen.add(digest);result['confirmed_contents'].append(entry)
                    if prior:
                        if apply:
                            prior['nsfw_confirmation']['paths']=sorted(set(prior['nsfw_confirmation'].get('paths',[prior['nsfw_confirmation']['path']]))|{str(path)})
                            if not Path(prior['nsfw_confirmation']['path']).exists():prior['nsfw_confirmation']['path']=str(path)
                            if prior.get('vault_transfer',{}).get('status')=='local_verified_exposed_copies_recycled':
                                prior['vault_transfer']['status']='pending_reconfirmed_input'
                            external.save(config,imported)
                        result['already_confirmed']+=1;continue
                    if apply:
                        if digest in settings.hashes(config['safe_ledgers']):raise ValueError('New wanted decision blocks approval')
                        if queue.fresh(path)!=actual:raise ValueError('External input changed before approval')
                        imported['items'][digest]={'origin_kind':'external_confirmed','status':'reviewing',
                            'bytes':actual['bytes'],'origins':[],
                            'nsfw_confirmation':{'path':str(path),'paths':[str(path)],'sha256':digest,'bytes':actual['bytes'],
                                'method':'user_placed_external_bytes_in_CONFIRMED','confirmed_utc':media.utc()},
                            'nsfw_privacy':{'decision':'hide_apple_move_onedrive_personal_vault','status':'pending_provider_actions'}}
                        external.save(config,imported)
                        media.journal(config['review_state'].parent/'review-actions.jsonl',
                            {'action':'external_nsfw_confirmed','sha256':digest,'bytes':actual['bytes'],'path':str(path),'utc':media.utc()})
                        result['new_confirmations']+=1
                    continue
                if item['status']!='reviewing':raise ValueError('Inactive review; cannot override a keep decision')
                if not any(e.get('category')=='nsfw' or e.get('analysis_check')=='nsfw' for e in item.get('evidence',[])):
                    raise ValueError('This is not a recorded NSFW review item')
                if {r['bytes'] for r in item['origins']}!={actual['bytes']}:raise ValueError('Recorded identity differs')
                media.inside(Path(item['review_path']),review)
                seen.add(digest)
                entry={'sha256':digest,'bytes':actual['bytes'],'confirmed_copy':str(path),
                    'apple_status':'reuse_exact_cloud_match_or_upload_confirmed_original_then_Private','onedrive_status':'pending_authenticated_personal_vault_transfer',
                    'recorded_origins':item['origins']}
                result['confirmed_contents'].append(entry)
                if item.get('nsfw_confirmation'):
                    if apply:
                        receipt=item['nsfw_confirmation'];receipt['paths']=sorted(set(receipt.get('paths',[receipt['path']]))|{str(path)})
                        if not Path(receipt['path']).exists():receipt['path']=str(path);item['review_path']=str(path)
                        if item.get('vault_transfer',{}).get('status')=='local_verified_exposed_copies_recycled':
                            item['vault_transfer']['status']='pending_reconfirmed_input'
                        queue.save_state(config['review_state'],state)
                    result['already_confirmed']+=1;continue
                if apply:
                    if digest in settings.hashes(config['safe_ledgers']):raise ValueError('New wanted decision blocks confirmation')
                    current=queue.fresh(path)
                    if (current['sha256'],current['bytes'])!=(digest,actual['bytes']):raise ValueError('Input changed before recording')
                    stamp=datetime.now(timezone.utc).isoformat()
                    item['nsfw_confirmation']={'path':str(path),'paths':[str(path)],'sha256':digest,'bytes':actual['bytes'],
                        'confirmed_utc':stamp,'method':'user_placed_exact_bytes_in_CONFIRMED',
                        'previous_review_path':item['review_path']}
                    item['review_path']=str(path)
                    item['nsfw_privacy']={'decision':'hide_apple_move_onedrive_personal_vault',
                        'status':'pending_provider_actions','updated_utc':stamp,
                        'apple_hidden_verified':False,'vault_transfer_verified':False}
                    queue.save_state(config['review_state'],state)
                    media.journal(config['review_state'].parent/'review-actions.jsonl',
                        {'action':'nsfw_confirmed_by_folder','sha256':digest,'path':str(path),'bytes':actual['bytes'],'utc':stamp,'cloud_actions':0})
                    result['new_confirmations']+=1
            except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
                result['held'].append({'path':str(path),'error':str(exc)})
        if apply:queue.write_index(config['review_state'],state)
    write_json(out/'report.json',result)
    lines=['# NSFW folder decisions','',
        f"New approvals recorded: {result['new_confirmations']}. Already recorded: {result['already_confirmed']}.",
        'Folder intake alone performs no Apple hiding, Vault transfer, gallery crawl, or model run.',
        'Run photos.py nsfw-review --apply --deliver --username YOUR_APPLE_ACCOUNT for the destination actions.',
        'On iPhone, open Private and hide its items. Private is an ordinary album until you use Hide; no re-upload or new photo is needed.',
        'Existing exact Apple originals (including Hidden) are reused; confirmed standalone originals missing from Apple may be uploaded once and verified. Filenames alone never authorize a link.',
        'External inputs have no invented gallery origins. Known gallery provenance remains recorded when a confirmed original is uploaded.',
        'Locked Vaults, missing matches, unknown upload outcomes and complete asset bundles stay pending. See the provider reports.','']
    for item in result['confirmed_contents']:
        lines += [f"Content: {item['sha256']}",f"Confirmed copy: {item['confirmed_copy']}",
            'Recorded originals: '+' | '.join(r['path'] for r in item['recorded_origins']),
            'Apple: '+item['apple_status']+'. OneDrive: '+item['onedrive_status']+'.','']
    lines += ['Holds: '+json.dumps(result['held'])]
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    return result


def deliver(config,username=None,provider=None,vault=None,recycler=None):
    import icloud_private_album as apple
    import personal_vault_transfer as microsoft
    result={}
    # Independent destinations have independent receipts. A failure must not
    # masquerade as both completed, nor silently skip the other destination.
    for name,action in (('apple_private',lambda:apple.run(config,provider,True,username=username)),
                        ('personal_vault',lambda:microsoft.run(config,True,vault,recycler))):
        try:result[name]=action()
        except Exception as exc:result[name]={'held':[{'error_type':type(exc).__name__}],'status':'pending'}
    write_json(config['output']/'nsfw-review'/'delivery-report.json',result)
    return result


def service(config,username=None):
    """Apply human inbox decisions once, without crawling local galleries.

    Background launches reuse authentication, but freshly verify every remaining
    confirmed identity. Apple holds do not prevent secured local transfers.
    Pending Apple work can read verified unlocked Vault bytes later. Locked
    Vaults and expired sessions remain explicit holds.
    """
    import icloud_private_album as apple
    import personal_vault_transfer as microsoft
    started=media.utc()
    import decision_inboxes
    aliases=decision_inboxes.normalize(config)
    restore_hashes=set()
    for restore_input in (config['restore_folder'],config.get('family_restore_folder',config['restore_folder'].parent/'RESTORE_FAMILY')):
        if not restore_input.exists():continue
        for path in restore_input.rglob('*'):
            if path.is_file():
                try:restore_hashes.add(queue.fresh(path)['sha256'])
                except (OSError,ValueError,subprocess.TimeoutExpired):pass
    active=config.get('active_galleries',['onedrive','icloud'])
    scoped={**config,'roots':{k:v for k,v in config['roots'].items() if k in active}}
    result=process(scoped,apply=True)
    result['decision_folder_transfers']=aliases
    result['held'].extend(aliases['held'])
    import review_deletions
    result['delete']=review_deletions.service(config,restore_hashes=restore_hashes)
    result['held'].extend({'inbox':'DELETE','nonblocking':True,**hold} for hold in result['delete']['held'])
    selected=apple.selections(config)
    delivery={}
    pending=list(selected)  # Every remaining CONFIRMED input gets fresh provider verification.
    # Gallery indexing/cleanup scope must not suppress explicit privacy delivery.
    if pending:
        try:
            account=config['output']/'icloud-session'/'account.json'
            username=username or config.get('icloud_username') or (json.loads(account.read_text(encoding='utf-8'))['username'] if account.exists() else None)
            print(json.dumps({'stage':'privacy_delivery','provider':'apple_private'}),flush=True)
            from bounded_privacy_service import run as bounded_delivery
            delivery['apple_private']=bounded_delivery('apple_private',config,apply=True,digests=pending,username=username,interactive=False)
            print(json.dumps({'stage':'privacy_delivery_finished','provider':'apple_private','status':delivery['apple_private'].get('status')}),flush=True)
        except Exception as exc:
            delivery['apple_private']={'status':'pending','held':[{'error_type':type(exc).__name__,
                'error':'Apple authentication/action could not finish. Run privacy-review.ps1 to sign in or inspect icloud-private/report.json.'}]}
    if selected:
        try:
            print(json.dumps({'stage':'privacy_delivery','provider':'personal_vault'}),flush=True)
            from bounded_privacy_service import run as bounded_delivery
            delivery['personal_vault']=bounded_delivery('personal_vault',{**config,'privacy_verification_after':started},apply=True)
            print(json.dumps({'stage':'privacy_delivery_finished','provider':'personal_vault','status':delivery['personal_vault'].get('status')}),flush=True)
        except Exception as exc:
            delivery['personal_vault']={'status':'pending','held':[{'error_type':type(exc).__name__,
                'error':'Vault transfer could not finish. Unlock Personal Vault and run privacy-review.ps1 -Resume.'}]}
    result['apple_actions_deferred']=False
    result['delivery']=delivery
    result['held'].extend({'provider':name,**hold} for name,report in delivery.items() for hold in report.get('held',[]))
    write_json(config['output']/'nsfw-review'/'delivery-report.json',delivery)
    write_json(config['output']/'nsfw-review'/'automatic-inbox-report.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup',action='store_true',help='Create NSFW/CONFIRMED and the existing RESTORE folder')
    parser.add_argument('--apply',action='store_true',help='Apply folder decisions; add --deliver for destination actions')
    parser.add_argument('--deliver',action='store_true',help='Add existing/new external Apple assets to Private and transfer standalone files into unlocked native Vault')
    parser.add_argument('--username',help='Apple Account; sign-in prompts appear only when required')
    parser.add_argument('--reject-sha256',action='append',help='User-declared false positive: restore and exclude only this exact content')
    args=parser.parse_args()
    if args.deliver and (not args.apply or args.setup):parser.error('--deliver requires --apply and cannot be combined with --setup')
    if args.reject_sha256 and (args.deliver or args.setup):parser.error('False-positive correction cannot deliver or set up privacy actions')
    config=settings.load();result=reject(config,args.reject_sha256,args.apply) if args.reject_sha256 else process(config,args.apply,args.setup)
    if args.deliver:result['delivery']=deliver(config,args.username)
    print(json.dumps(result));return 2 if result['held'] or any(r.get('held') for r in result.get('delivery',{}).values()) else 0


if __name__=='__main__':raise SystemExit(main())
