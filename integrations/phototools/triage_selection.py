"""Read-only, bounded batches over explicit current paths. No inbox actions."""
import argparse
import json
from pathlib import Path
from photo_triage.batch import batches


def run(selection, output, config, batch_size=500, limit=None):
    from photo_pass import BoundedProcessor
    from photo_settings import decisions
    import scan_reference_photos as scan
    rows=json.loads(Path(selection).read_text(encoding='utf-8-sig'))
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    protected,deleted=decisions(config['safe_ledgers'],config['deletion_ledger'])
    # One persistent worker and model. Resume only successful current-content results.
    worker=BoundedProcessor({**config,'download_originals':False}, {'junk','quality'},
                            output/'triage-cache.sqlite',protected,deleted,300)
    report={'actions':0,'batches':0,'files':[],'holds':[],'pending':0,'new_attempts':0,'cache_hits':0}
    maximum=len(rows) if limit is None else limit
    if maximum<1:raise ValueError('limit must be positive')
    try:
        for batch in batches(rows,batch_size):
            for item in batch:
                path=Path(item['path']).resolve()
                try:
                    st=path.stat()
                    kind='photo' if path.suffix.lower() in scan.PHOTOS else 'video' if path.suffix.lower() in {'.mp4','.mov','.m4v','.avi','.mkv','.webm'} else 'unsupported'
                    row={'path':str(path),'root':str(path.parent),'library':item['library'],
                         'kind':kind,'bytes':st.st_size,'mtime_ns':st.st_mtime_ns,
                         'placeholder':bool(getattr(st,'st_file_attributes',0)&scan.NONRESIDENT),
                         'status':'discovered'}
                    value=worker.process_row(row)
                    report['files'].append(value)
                    reused=all(c.get('cache_reused') for c in value.get('checks',{}).values())
                    report['cache_hits']+=int(reused)
                    report['new_attempts']+=int(not reused)
                except Exception as exc:
                    report['holds'].append({'path':str(path),'error':str(exc)})
                    report['new_attempts']+=1
                if report['new_attempts']>=maximum:break
            report['batches']+=1
            report['pending']=len(rows)-len(report['files'])-len(report['holds'])
            from triage_adapter import write_similarity_report
            write_similarity_report(report['files'],output/'visual-candidates.json')
            (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps({'batch':report['batches'],'processed':len(report['files']),'pending':report['pending']}),flush=True)
            if report['new_attempts']>=maximum:break
    finally:worker.close()
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--batch-size',type=int,default=500)
    parser.add_argument('--limit',type=int)
    args=parser.parse_args()
    import photo_settings
    report=run(args.selection,args.output,photo_settings.load(args.config),args.batch_size,args.limit)
    return 2 if report['holds'] or any(any(c.get('status') not in {'analyzed','not_applicable','held_user_exclusion','held_intentional_deletion'} for c in r.get('checks',{}).values()) for r in report['files']) else 0


if __name__=='__main__':raise SystemExit(main())
