"""Retry current-run review moves with fresh identity and current user decisions."""
import argparse
import json
from pathlib import Path
import photo_settings as settings
import review_queue as queue
import review_routes
from two_library_duplicates import write_json


def run(root,apply=False):
    root=Path(root);out=root/'review-repair';out.mkdir(exist_ok=True)
    config=settings.load()
    from restore_family import before_run
    before_run(config)
    summary=json.loads((root/'workflow-summary.json').read_text())
    paths={p for g in summary['scan']['streaming_cleanup']['held'] for p in g['paths']}
    paths.update(r['path'] for r in summary['verification']['holds'])
    with (root/'files.jsonl').open(encoding='utf-8') as stream:
        originals={r['path']:r for r in map(json.loads,stream) if r['path'] in paths}
    rows=[];issues=[];absent=[]
    from photo_pass import download_original
    from audit_two_libraries import NONRESIDENT
    for path,row in originals.items():
        if not Path(path).exists():absent.append(path);continue
        try:
            stat=Path(path).stat()
            current={**row,'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,
                     'placeholder':bool(getattr(stat,'st_file_attributes',0)&NONRESIDENT)}
            if current['placeholder']:
                if not apply:issues.append({'path':path,'error':'Download required before apply'});continue
                current=download_original(current)
            identity=queue.fresh(Path(path))
            if (identity['sha256'],identity['bytes'])!=(row['sha256'],row['bytes']):
                raise ValueError('Source differs from recorded review selection')
            current.update(identity);current['placeholder']=False
            rows.append(current)
        except Exception as exc:issues.append({'path':path,'error':str(exc)})
        write_json(out/'preparation.json',{'verified':len(rows),'absent':len(absent),'issues':issues})
    result=review_routes.route(config,rows,out,apply=apply,verify=apply)
    result.update(preparation_issues=issues,already_absent_sources=absent)
    write_json(out/'repair-summary.json',result)
    print(json.dumps(result))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();run(args.run,args.apply)
