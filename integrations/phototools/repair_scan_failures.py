"""Repair current-run failed checks using current bytes and verified duplicate handoffs."""
import argparse
import copy
import json
import os
from pathlib import Path
import time
from contextlib import closing
from collections import defaultdict
import photo_settings as settings
from photo_pass import BoundedProcessor
from photo_pipeline import alias_result
from duplicate_scan_handoff import reconcile,resolved
from streaming_cleanup import StreamingCleanup
from live_nsfw_review import dependency,read_new
from two_library_duplicates import write_json


def failed(row):return any(c.get('status')=='error' for c in row.get('checks',{}).values())


def repair_group_key(row):
    # An interrupted worker may not have returned a hash. Equal sizes are not
    # identity and must never share inference between those unverified paths.
    return (row.get('sha256') or ('unhashed',row['path']),row['bytes'])


def verify_repair(owner,value):
    sha=value.get('sha256')
    if not sha or (owner.get('sha256') and sha!=owner['sha256']):
        raise ValueError('Repair could not verify source identity')
    if not owner.get('sha256'):
        if any(value.get(k)!=owner.get(k) for k in ('path','bytes','mtime_ns')) or value.get('hash_evidence')!='full_scan_hash_unchanged_size_mtime':
            raise ValueError('Unhashed repair needs a fresh unchanged-file hash')


def run(root,owner_pid,retry_errors=False):
    root=Path(root);out=root/'failure-repair';out.mkdir(exist_ok=True)
    config={**settings.load(),'single_gallery_cleanup':True,'analysis_scope':'separate_galleries',
            'download_originals':False,'model_threads':1,'pass_workers':1,
            'video_checkpoint_root':out/'video-checkpoints','video_force_ffmpeg':True}
    from restore_family import before_run
    before_run(config)
    checks=['junk','nsfw','quality','tiktok','duplicates','live-photos']
    inventory=[json.loads(l) for l in (root/'inventory.jsonl').open(encoding='utf-8')]
    handler=StreamingCleanup(config,out,checks=['junk','nsfw','quality','tiktok'])
    handler.start(inventory,[],False,{})
    records={};results={};worker=None;policy=None
    result_path=out/'results.jsonl'
    if result_path.exists():
        with result_path.open(encoding='utf-8') as stream:
            for r in read_new(stream):results[r['path']]=r
        if retry_errors:results={p:r for p,r in results.items() if not failed(r)}
    def save(status):
        write_json(out/'progress.json',{'status':status,'pid':os.getpid(),'source_pid':owner_pid,
            'resolved_files':sum(failed(r) and p in results and not failed(results[p]) for p,r in records.items()),
            'remaining_failed_files':sum(failed(r) and (p not in results or failed(results[p])) for p,r in records.items()),
            'original_failed_files':sum(failed(r) for r in records.values()),
            'actions':handler.stats,'updated_at':time.time()})
    try:
        with (root/'coverage.jsonl').open(encoding='utf-8') as stream,result_path.open('a',encoding='utf-8') as output:
            while True:
                for row in read_new(stream):
                    records[row['path']]=row;handler.prepared(row)
                    handler.accept(results.get(row['path'],dependency(row)))
                groups=defaultdict(list)
                for row in records.values():
                    if failed(row) and row['path'] not in results:
                        groups[repair_group_key(row)].append(row)
                for key,group in sorted(groups.items(),key=lambda item:item[1][0]['kind']=='video'):
                    save('repairing')
                    try:
                        repaired=reconcile(copy.deepcopy(group),root)
                        owner=min((r for r in repaired if not r.get('early_move')),key=lambda r:r['path'].casefold())
                        protected,deleted=settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
                        current_policy=(frozenset(protected),frozenset(deleted))
                        if policy!=current_policy:
                            if worker:worker.close()
                            worker=BoundedProcessor(config,checks,out/'cache.sqlite',protected,deleted,300)
                            policy=current_policy
                        # Resume only when a bounded attempt saved additional samples.
                        # No progress means no repeated timeout loop.
                        def checkpoint_signature():
                            return sorted((str(p),p.stat().st_mtime_ns) for p in config['video_checkpoint_root'].rglob('*.json'))
                        for attempt in range(3):
                            before=checkpoint_signature()
                            value=worker.process_row({**owner,'_file_processing_seconds':0})
                            timeout=any('deadline' in c.get('error','').lower() for c in value.get('checks',{}).values())
                            if not timeout or before==checkpoint_signature():break
                            if attempt<2:time.sleep(2**(attempt+1))
                        verify_repair(owner,value)
                        for row in repaired:
                            updated=(resolved(row,checks) if row.get('duplicate_keeper') else
                                     dict(value) if row['path']==owner['path'] else
                                     alias_result(value,row,checks,preserve_metadata=True))
                            updated['failure_repair']={'source':owner['path'],'fresh_sha256':value['sha256']}
                            results[row['path']]=updated
                            output.write(json.dumps(updated)+'\n');output.flush();os.fsync(output.fileno())
                            handler.accept(updated)
                    except Exception as exc:
                        for row in group:
                            updated={**row,'repair_error':str(exc)};results[row['path']]=updated
                            output.write(json.dumps(updated)+'\n');output.flush()
                    save('running')
                from photo_pass_resume import process_alive
                # Release model memory while following an otherwise healthy scan.
                if worker:
                    worker.close();worker=None;policy=None
                if not process_alive(owner_pid):
                    handler.preparation_finished();break
                save('following');time.sleep(5)
    finally:
        if worker:worker.close()
        handler.close();save('finished')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True);parser.add_argument('--owner-pid',type=int,required=True)
    parser.add_argument('--retry-errors',action='store_true',help='One new attempt after correcting a reported repair error')
    args=parser.parse_args();run(args.run,args.owner_pid,args.retry_errors)
