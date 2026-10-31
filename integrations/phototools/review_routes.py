"""Move model nominations into human review using existing verified executors."""
import argparse
import json
from pathlib import Path
import subprocess

import folder_ingest as ingest
import photo_settings as settings
import review_queue as queue
from two_library_duplicates import read_scan,write_json


def supported_triage_finding(finding):
    """Accept the new provider's validated category evidence, including video samples."""
    if not finding.get('candidate'):
        return False
    frames=finding.get('frame_evidence')
    if frames:
        return (finding.get('status')=='analyzed'
                and len(frames)==finding.get('samples_requested')
                and all(f.get('status') in {'analyzed','not_applicable'} for f in frames)
                and any(supported_triage_finding(f) and f.get('category')==finding.get('category') for f in frames))
    evidence=finding.get('contextual_evidence',{})
    if evidence.get('source')!='photo_triage_agent':
        return False
    from photo_triage.vision import junk_result
    try:
        validated=junk_result(evidence, finding.get('dimensions'))
        return validated['candidate'] and validated['category']==finding.get('category')
    except (ValueError,TypeError):
        return False


def existing_review(config,group,state,categories=('junk','quality','tiktok')):
    """Replay a current review identity, never infer a decision from a missing file."""
    from privacy_storage import verify
    result=[]
    for row in group:
        item=state['items'].get(row.get('sha256'),{})
        if item.get('status')!='reviewing' or item.get('representative',{}).get('bytes')!=row.get('bytes'):
            result.append(row);continue
        category=next((e.get('analysis_check',e.get('category')) for e in item.get('evidence',[])
                       if e.get('analysis_check',e.get('category')) in categories),None)
        if not category or item.get('nsfw_confirmation'):
            result.append(row);continue
        path=Path(item['review_path'])
        import merge_photos as media
        media.inside(path,config['nsfw_review_folder'] if category=='nsfw' else config['review_folder'])
        verify(path,row['sha256'],row['bytes'])
        reason=item.get('script_reason','unknown')
        finding={'status':'analyzed','candidate':True,'category':reason,'reasons':[reason],
                 'policy':'existing-review','confidence_band':'supported_review'}
        result.append({**row,'checks':{**row.get('checks',{}),category:finding}})
    return result


def verify_moves(config,out):
    """Freshly verify completed move receipts without a gallery crawl or mutation."""
    import merge_photos as media
    out=Path(out);journal=out/'actions.jsonl'
    events=[json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()] if journal.exists() else []
    completed={event['source']:event for event in events if event.get('action')=='folder_review_moved_verified'}
    result={'verified_receipts':0,'absent_sources':0,'returned_sources':[],'held':[],
            'gallery_crawls':0,'photo_mutations':0,'cloud_deletion_verified':False}
    for source,event in completed.items():
        try:
            identity=(event['sha256'],event['bytes'])
            restored_by_decision=False
            target=Path(event['destination'])
            media.inside(target,config['review_folder'])
            if not target.exists():
                item=queue.load_state(config['review_state'])['items'].get(event['sha256'],{})
                restored=item.get('restore_destination',{})
                if item.get('status')=='kept' and (restored.get('sha256'),restored.get('bytes'))==identity:
                    target=Path(restored['path']);restored_by_decision=True
                    if not any(target.is_relative_to(root) for root in config['roots'].values()):raise ValueError('Restored location outside galleries')
                elif item.get('status')=='deleted':
                    from review_deletions import intents
                    request=intents(config)['items'].get(event['sha256'],{})
                    if request.get('status')=='deleted_locally_verified' and request.get('bytes')==event['bytes']:
                        target=None  # Explicit deletion has no retained media keeper.
                        result['verified_deleted_decisions']=result.get('verified_deleted_decisions',0)+1
            for path in ([target] if target is not None else []):
                actual=queue.fresh(path)
                if (actual['sha256'],actual['bytes'])!=identity:raise ValueError('Preserved review bytes differ')
            result['verified_receipts']+=1
            if Path(source).exists() and not restored_by_decision:
                result['returned_sources'].append(source)
                raise ValueError('Source path returned after move; provider removal remains pending')
            if restored_by_decision:result['verified_restored_decisions']=result.get('verified_restored_decisions',0)+1
            else:result['absent_sources']+=1
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
            result['held'].append({'path':source,'error':str(exc)})
    write_json(out/'verification.json',result)
    return result


def route(config,rows,out,apply=False,recycler=None,checks=('junk','tiktok','quality'),verify=False):
    from privacy_routes import hashes as private_hashes
    from delete_icloud_clutter import modern_recycler
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    result={'moved_files':0,'review_contents':0,'planned_files':0,'held':[],
            'gallery_crawls':0,'model_runs':0,'permanent_deletions':0}
    wanted,deleted=settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
    private=private_hashes(config)
    protected=wanted|deleted|private
    state=queue.load_state(config['review_state'])
    if apply and recycler is None:recycler=modern_recycler()
    groups=ingest.groups(rows)
    if apply:
        from review_categories import create
        create(config['review_folder'])
    # Put the categories Steve asked about first, without changing classifications.
    groups.sort(key=lambda group:min((0 if r.get('checks',{}).get('junk',{}).get('category') in
        {'meme_or_cartoon','receipt','screenshot'} else 1) for r in group))
    for number,group in enumerate(groups,1):
        from scan_lifecycle import check_stop
        for parent in (out,*list(out.parents)[:3]):check_stop(parent)
        try:
            group=existing_review(config,group,state)
        except (OSError,ValueError,KeyError,subprocess.TimeoutExpired) as exc:
            result['held'].append({'paths':[r['path'] for r in group],'error':str(exc)})
            continue
        def nominated(row,check):
            finding=row.get('checks',{}).get(check,{})
            return finding.get('candidate') and (check!='junk' or supported_triage_finding(finding) or
                finding.get('policy') in {'existing-review','supported-junk-v1','supported-junk-v2-commercial-and-meme-evidence','supported-junk-v3-captioned-memes','supported-junk-v4-presentations','supported-junk-v5-distinctive-graphic-captions','supported-junk-v6-caption-and-web-evidence','supported-junk-v7-corroborated-graphics'} and finding.get('confidence_band')=='supported_review')
        selected=next((c for c in checks if any(nominated(r,c) for r in group)),None)
        if selected is None and any(r.get('checks',{}).get('junk',{}).get('candidate') for r in group):
            result['held'].append({'paths':[r['path'] for r in group],
                'error':'Old or weak junk nomination requires fresh analysis; no move authorized'})
        if selected is None:continue
        if any(r.get('early_move') for r in group):continue
        if any(r.get('sha256') in protected or r.get('checks',{}).get('nsfw',{}).get('candidate') for r in group):continue
        present=[r for r in group if Path(r['path']).exists()]
        if not present:continue
        try:
            if selected in {'junk','tiktok'}:
                import family_faces
                bank=family_faces.load(config)
                if bank['items'] and any(r.get('kind') in {'photo','video'} and
                        family_faces.inspect_path(config,Path(r['path'])).get('matched') for r in present):
                    result['family_protected']=result.get('family_protected',0)+len(present)
                    continue
            if any(not r.get('sha256') for r in group):raise ValueError('Complete companion bytes are not available')
            if any(r.get('native_live_photo') and r.get('library')=='icloud' for r in present):
                raise ValueError('Apple-managed Live Photo motion needs complete preservation before a review move')
            if not apply:result['planned_files']+=len(present);continue
            # Current identity, rather than an old path or timestamp, authorizes
            # reusing the saved model nomination. No gallery/model scan here.
            current=[]
            for row in present:
                actual=queue.fresh(Path(row['path']))
                if (actual['sha256'],actual['bytes'])!=(row['sha256'],row['bytes']):raise ValueError('Candidate bytes changed')
                current.append({**row,**actual})
            moved=ingest.review_group(config,current,selected,out,
                config['preservation']/'review-nominations',out/'actions.jsonl',recycler,
                selection_file=out/'current-review.jsonl',write_index=False)
            result['moved_files']+=moved;result['review_contents']+=len({r['sha256'] for r in current})
        except (OSError,ValueError,RuntimeError,KeyError,subprocess.TimeoutExpired) as exc:
            result['held'].append({'paths':[r['path'] for r in present],'error':str(exc)})
        write_json(out/'summary.json',result)
        print(json.dumps({'stage':'move_to_FOR_REVIEW','moved_files':result['moved_files'],
                          'held_groups':len(result['held'])}),flush=True)
    if apply:
        with queue.locked(config['review_state']):queue.write_index(config['review_state'],queue.load_state(config['review_state']))
        if verify:result['verification']=verify_moves(config,out)
    write_json(out/'summary.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--verify',action='store_true',help='Fresh read-only verification of recorded review moves')
    args=parser.parse_args();config=settings.load()
    if args.verify:
        result=verify_moves(config,args.run/'review-nominations')
        print(json.dumps(result));return 2 if result['held'] else 0
    with queue.locked(args.run/'review-routing-state.json'):
        result=route(config,read_scan(args.run/'files.jsonl'),args.run/'review-nominations',args.apply,verify=args.apply)
        print(json.dumps(result));return 2 if result['held'] or result.get('verification',{}).get('held') else 0


if __name__=='__main__':raise SystemExit(main())
