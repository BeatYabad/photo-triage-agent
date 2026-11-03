"""Recheck a current run's duplicate candidates without restarting its scan."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sqlite3
import photo_settings
import review_queue as queue
import two_library_duplicates as duplicates


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();config=photo_settings.load(args.config)
    original=json.loads(args.plan.read_text());args.output.mkdir(parents=True,exist_ok=False)
    resolved={'moves':[]};report={'examined':0,'eligible':0,'held':[],'cloud_deletion_verified':False}
    families={}
    with closing(queue.FingerprintWorker(timeout=45,allow_cloud=False)) as reader:
        with queue.locked(config['review_state']):
            state=queue.load_state(config['review_state'])
            wanted,deleted=photo_settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
            for old in original['moves']:
                if old.get('hold') not in {
                    'motion_or_variant_companion', 'Asset filename/path mismatch',
                    'Unmapped path has a native filename match; provider mapping pending',
                    'iCloud database required for asset-aware duplicate moves',
                }:continue
                report['examined']+=1
                try:
                    item=state['items'].get(old['sha256'],{})
                    if item.get('status') in {'reviewing','copy_pending','restore_pending','delete_pending'} or old['sha256'] in deleted-wanted:
                        raise ValueError('Current review/deletion decision takes precedence')
                    row=duplicates.verify_row(reader,old,config['roots'])
                    keeper=duplicates.verify_row(reader,old['keeper'],config['roots'])
                    for member in (row,):
                        parent=Path(member['path']).parent
                        if parent not in families:
                            families[parent]=[{'path':str(p),'library':member['library']} for p in parent.iterdir() if p.is_file()]
                        hold=duplicates.companion_hold(member,families[parent],config.get('icloud_database'))
                        if hold:raise ValueError(hold)
                    row.update(keeper=keeper,hold=None,asset_evidence=None)
                    resolved['moves'].append(row)
                except (OSError,ValueError,sqlite3.Error,subprocess.SubprocessError) as exc:
                    report['held'].append({'path':old['path'],'error':str(exc)})
            report['eligible']=len(resolved['moves'])
            duplicates.write_json(args.output/'plan.json',resolved)
            if args.apply:
                report['actions']=duplicates.apply_moves(resolved,config,args.output,config['roots'],reader)
                report['verification']=duplicates.verify_batch(config,args.output,config['roots'],reader)
    duplicates.write_json(args.output/'summary.json',report)
    if args.apply:
        target=args.plan.parent/'resolution.json'
        prior=json.loads(target.read_text()) if target.exists() else {}
        reports=sorted(set(prior.get('reports',[]))|{str(args.output/'summary.json')})
        recycled=sum(json.loads(Path(p).read_text()).get('verification',{}).get('verified_destination_only',0) for p in reports)
        initial=max(prior.get('original_holds',0),sum(bool(r.get('hold')) for r in original['moves']))
        duplicates.write_json(target,{'reports':reports,'original_holds':initial,
            'verified_recycled':recycled,'remaining_holds':max(0,initial-recycled)})
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
