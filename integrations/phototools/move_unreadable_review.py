"""Move explicitly selected unreadable originals using verified review receipts."""
import argparse
import json
from pathlib import Path
import photo_settings as settings
import review_queue as queue
import review_routes
from photo_triage.vision import junk_result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    selections={r['path']:r for r in map(json.loads,(args.run/'classification-recheck-latest.jsonl').open())
                if r['result'].get('nomination_reason')=='user_keep_unreadable_original'}
    rows=[]
    for row in map(json.loads,(args.run/'files.jsonl').open()):
        if row['path'] not in selections:continue
        finding=selections[row['path']]['result']
        actual=queue.fresh(Path(row['path']))
        if (actual['sha256'],actual['bytes'])!=(row['sha256'],row['bytes']):
            raise ValueError('Selected original changed: '+row['path'])
        evidence=finding['contextual_evidence']
        result=junk_result(evidence,finding['dimensions'],finding.get('frames',1))
        if not result['candidate'] or result['category']!='unreadable_original':
            raise ValueError('Unreadable review evidence not accepted')
        rows.append({**row,**actual,'checks':{'junk':result}})
    if len(rows)!=len(selections):raise ValueError('Some selected originals have no provenance')
    result=review_routes.route(settings.load(),rows,args.run/'unreadable-review',
                               apply=args.apply,checks=('junk',),verify=args.apply)
    print(json.dumps(result))


if __name__=='__main__':main()
