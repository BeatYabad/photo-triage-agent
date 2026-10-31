"""One entry point for local photo tools; use 'photos.py tasks' to start."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import photo_settings as settings

HERE = Path(__file__).resolve().parent
TASKS = {
    'restore-family': 'Restore RESTORE_FAMILY photos and learn local faces for junk protection only',
    'triage-batch': 'Read-only resumable LFM/quality batches over explicit paths; no cleanup',
    'compare-selection': 'Read-only Czkawka comparison of explicit image paths; no cleanup or inbox actions',
    'merge-only-duplicates': 'Move every supplied file into Camera Roll; recycle exact duplicates, without any other filters',
    'review-reasons': 'Count current FOR_REVIEW files by recorded reasons; read-only, without model runs',
    'review-decisions': 'Apply RESTORE, DELETE and NSFW/CONFIRMED folder decisions',
    'review-pending': 'Move flagged files from a completed run into FOR_REVIEW without another model scan',
    'status': 'Show current cleanup progress without scanning or processing review decisions',
    'stop': 'Stop a running pass; discard scan partials and retain completed move receipts',
    'nsfw-search': 'Fresh search of both galleries; move NSFW into review with verified preservation',
    'ingest-folder': 'Check NSFW, junk and duplicates once; move private files to NSFW, then sort/import eligible files',
    'process-gallery': 'Complete gallery processing: all cleanup checks, NSFW moves and CONFIRMED/RESTORE decisions',
    'import': 'Filter IMPORT once; verify iCloud bytes before clearing successful inputs',
    'process-folder': 'Filter a supplied folder; move passes to one gallery, flags to review, delete exact duplicates',
    'pass': 'Crawl once; run all selected local checks with shared decoding and caches',
    'cleanup': 'Search both galleries, share review actions, then repair retained-media gaps both ways',
    'live-photo-cleanup': 'Keep complete native Live Photos; remove verified separate component imports',
    'duplicates': 'Automatically recycle exact photo/video extras within each gallery; check cross-gallery gaps separately',
    'audit': 'Freshly compare resident iCloud and Camera Roll originals',
    'sync': 'Plan missing copies; --apply copies and freshly compares again',
    'references': 'Local CLIP ranking of clutter and temporary references',
    'nsfw': 'Local Marqo scene screening of images and sampled video frames',
    'nsfw-model-test': 'Read-only comparison of Marqo and NudeNet on explicit files or saved inventory',
    'nsfw-policy-test': 'Read-only labeled regression test of the nudity/sexual-content policy',
    'nsfw-folder': 'Scan one Camera Roll folder; --apply moves candidates into NSFW for review',
    'quality': 'Flag possible blur, extreme exposure and low resolution locally',
    'collect-quality': 'Copy one representative per flagged hash for review',
    'move-quality': 'Move selected OneDrive quality originals; retain verified duplicates',
    'undo-quality-review': 'Restore FOR_REVIEW originals and cancel this review without keeps',
    'collect-references': 'Copy one representative per reference hash for review',
    'collect-nsfw': 'Copy one representative per NSFW hash for review',
    'nsfw-vault': 'Prepare confirmed NSFW for Personal Vault; record decisions and block reimport',
    'nsfw-review': 'Process CONFIRMED and RESTORE; --deliver requests Apple Private and native Vault',
    'nsfw-private': 'Reuse exact Apple assets or upload missing confirmed originals into Private',
    'nsfw-vault-transfer': 'Transfer standalone confirmed assets into unlocked native Personal Vault',
    'restore': 'Restore recorded original locations; unknown origins go to dated Camera Roll; remember keeps',
    'review-status': 'Show pending reviews and permanent keep decisions',
    'relocate-review': 'Move recorded review copies to the configured folder',
    'jpeg': 'Create JPEG upload derivatives without replacing originals',
    'organize': 'Plan YYYY/MM organization; --apply moves with preservation',
    'merge': 'Merge an explicitly supplied source into Camera Roll',
    'tool': 'Run an existing maintained tool; use TOOL --help for its options'
}

def command(task, config, extra):
    if task=='process-gallery':task='ingest-folder' if '--folder' in extra else 'cleanup'
    if task == 'tool':
        if not extra: raise ValueError('Specify a maintained tool name')
        tool = HERE/(extra[0].removesuffix('.py')+'.py')
        if tool.parent != HERE or not tool.is_file() or tool.stem.startswith('test_') or tool.stem=='photos':
            raise ValueError('Unknown maintained tool')
        return [sys.executable,str(tool),*extra[1:]]
    roots = config['roots']; out = config['output']/task
    if task in ('references','nsfw','quality','collect-quality','collect-references','collect-nsfw'):
        scope=argparse.ArgumentParser(add_help=False)
        scope.add_argument('--gallery',action='append',choices=['icloud','onedrive','amazon'])
        selected,extra=scope.parse_known_args(extra)
        if selected.gallery:roots={label:roots[label] for label in dict.fromkeys(selected.gallery)}
    base = [sys.executable]
    if task=='triage-batch':return base+[str(HERE/'triage_selection.py'),*extra]
    if task=='compare-selection':return base+[str(HERE/'compare_selection.py'),*extra]
    if task=='merge-only-duplicates':return base+[str(HERE/'merge_only_duplicates.py'),
        '--destination',str(roots['onedrive']),'--output',str(out),*extra]
    if task=='review-reasons':return base+[str(HERE/'review_reasons.py'),'--review',str(config['review_folder']),
        '--state',str(config['review_state']),'--output',str(config['output']/'review-reasons.json'),*extra]
    if task=='review-decisions':return base+[str(HERE/'nsfw_review.py'),*extra]
    if task=='import':return base+[str(HERE/'import_inbox.py'),*extra]
    if task=='review-pending':return base+[str(HERE/'review_routes.py'),*extra]
    if task=='process-folder':return base+[str(HERE/'process_folder.py'),*extra]
    if task=='stop':return base+[str(HERE/'scan_lifecycle.py'),*extra]
    if task=='ingest-folder':return base+[str(HERE/'folder_ingest.py'),*extra]
    if task=='pass':return base+[str(HERE/'photo_pass.py'),*extra]
    if task=='nsfw-search':return base+[str(HERE/'nsfw_search.py'),*extra]
    if task=='nsfw-model-test':return base+[str(HERE/'nsfw_model_test.py'),*extra]
    if task=='nsfw-policy-test':return base+[str(HERE/'nsfw_policy_test.py'),*extra]
    if task=='cleanup':return base+[str(HERE/'photo_cleanup.py'),*extra]
    if task=='nsfw-vault':return base+[str(HERE/'nsfw_vault.py'),*extra]
    if task=='nsfw-review':return base+[str(HERE/'nsfw_review.py'),*extra]
    if task=='nsfw-private':return base+[str(HERE/'icloud_private_album.py'),*extra]
    if task=='nsfw-folder':return base+[str(HERE/'nsfw_folder_review.py'),*extra]
    if task=='nsfw-vault-transfer':return base+[str(HERE/'personal_vault_transfer.py'),*extra]
    if task=='duplicates':return base+[str(HERE/'two_library_duplicates.py'),*extra]
    if task=='live-photo-cleanup':return base+[str(HERE/'live_photo_cleanup.py'),*extra]
    if task=='move-quality':return base+[str(HERE/'move_quality_review.py'),*extra]
    if task=='undo-quality-review':return base+[str(HERE/'undo_quality_review.py'),*extra]
    safe = [arg for p in config['safe_ledgers'] for arg in ('--exclusions',str(p))]
    mapping = [arg for k,p in roots.items() for arg in ('--root',k+'='+str(p))]
    if task == 'audit':
        return base+[str(HERE/'audit_two_libraries.py'),'--icloud',str(roots['icloud']),
            '--onedrive',str(roots['onedrive']),'--output',str(out),
            '--deletions',str(config['deletion_ledger']), *safe, *extra]
    if task == 'sync':
        args = ['--icloud',str(roots['icloud']),'--onedrive',str(roots['onedrive']),
                '--output',str(out),'--deletions',str(config['deletion_ledger']),
                '--delivery-ledger',str(config['delivery_ledger'])]
        for p in config['safe_ledgers']: args += ['--safe-ledger',str(p)]
        if config.get('icloud_database'): args += ['--database',str(config['icloud_database'])]
        return base+[str(HERE/'sync_local_photo_folders.py'),*args,*extra]
    if task in ('references','nsfw','quality'):
        model_args=[] if task=='quality' else ['--model',str(config['clip_model'])] if task=='references' else []
        if task=='nsfw':
            model,backend,threshold=settings.nsfw_options(config)
            model_args=['--model',str(model),'--nsfw-backend',backend,'--nsfw-threshold',str(threshold)]
        args = [str(HERE/'scan_reference_photos.py'),'--analysis','reference' if task=='references' else task,
                *model_args,'--output',str(out),*mapping,*safe]
        for p in config['dependencies']: args += ['--dependencies',str(p)]
        return base+args+extra
    if task in ('collect-quality','collect-references','collect-nsfw','restore','review-status','relocate-review'):
        if task=='restore':mapping=[arg for k,p in settings.review_roots(config).items() for arg in ('--root',k+'='+str(p))]
        action='collect' if task.startswith('collect-') else {'restore':'restore','relocate-review':'relocate'}.get(task,'status')
        args=[str(HERE/'review_queue.py'),action,'--state',str(config.get('review_state',config['output']/'review/review-state.json')),
              '--review',str(config.get('review_folder',settings.review_default('FOR_REVIEW'))),
              '--restore',str(config.get('restore_folder',settings.review_default('RESTORE'))),*mapping,*safe]
        if action=='collect':args+=['--scan',str(config['output']/task.removeprefix('collect-')/'coverage.jsonl')]
        if task=='collect-nsfw':args+=['--review',str(config.get('nsfw_review_folder',settings.review_default('NSFW')))]
        extra=['--review' if v=='--destination' else v for v in extra]
        return base+args+extra
    if task == 'jpeg':
        return base+[str(HERE/'make_jpeg_derivatives.py'),'--output',str(out),*extra]
    if task == 'organize':
        return base+[str(HERE/'organize_dates.py'),'--root',str(roots['onedrive']),
                     '--preservation',str(config['preservation']),'--output',str(out),'--undated','mtime',*extra]
    if task == 'merge':
        return base+[str(HERE/'merge_photos.py'),'--destination',str(roots['onedrive']),
                     '--preservation',str(config['preservation']),'--output',str(out),*extra]
    raise ValueError('Unknown task')

def main():
    p = argparse.ArgumentParser(description=__doc__,add_help=False)
    p.add_argument('--config',type=Path,default=settings.DEFAULT)
    p.add_argument('task',choices=['tasks','check',*TASKS],nargs='?')
    a, extra = p.parse_known_args()
    if '--exclude-import' in extra:
        os.environ['PHOTOS_EXCLUDE_IMPORT']='1'
        extra=[v for v in extra if v!='--exclude-import']
    if a.task is None:
        p.print_help();return 0 if '--help' in extra or '-h' in extra else 1
    if a.task == 'tasks':
        for task, description in TASKS.items(): print(task.ljust(12)+description)
        return 0
    if a.task=='tool' and ('--help' in extra or '-h' in extra):
        return subprocess.run(command(a.task,{},extra)).returncode
    config = settings.load(a.config)
    if a.task=='merge-only-duplicates':
        return subprocess.run(command(a.task,config,extra)).returncode
    if a.task=='status':
        parser=argparse.ArgumentParser();parser.add_argument('--output',required=True,type=Path)
        args=parser.parse_args(extra)
        from cleanup_status import show
        show(args.output);return 0
    os.environ['PHOTOS_CONFIG'] = str(a.config.resolve())
    for dep in reversed(config['dependencies']): sys.path.insert(0,str(dep))
    if a.task=='restore-family':
        import restore_family
        argparse.ArgumentParser(description=restore_family.__doc__).parse_args(extra)
        result=restore_family.process(config);print(json.dumps(result),flush=True)
        return 2 if result['held'] or result['enrollment_gaps'] else 0
    if a.task in {'process-gallery','cleanup'} and '--folder' not in extra and not any(v in extra for v in ('--help','-h','--reuse','--resume')) and not os.environ.get('PHOTOS_UNATTENDED_CHILD'):
        import unattended_cleanup
        parser=argparse.ArgumentParser(add_help=False);parser.add_argument('--output',required=True,type=Path)
        args,remaining=parser.parse_known_args([v for v in extra if v!='--unattended'])
        return unattended_cleanup.run(a.config.resolve(),args.output,remaining)
    if a.task == 'check':
        wanted,deleted = settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
        status = {k: p.is_dir() for k,p in config['roots'].items()}
        model,backend,threshold=settings.nsfw_options(config)
        models={'clip':config['clip_model'].is_dir(),'nsfw':all((model/n).is_file() for n in ('model.safetensors','config.json')) if backend=='marqo' else model.is_file()}
        if backend=='marqo':models['nsfw_nudity_verifier']=config['nsfw_model'].is_file()
        print(json.dumps({'roots_present':status,'wanted_hashes':len(wanted),'deleted_hashes':len(deleted),
                          'models_present':models,'exiftool_present':config['exiftool'].is_file()},indent=2))
        if not (all(status.values()) and all(models.values()) and config['exiftool'].is_file()):return 2
        from runtime_health import check,RuntimeProbeFailed
        try:print(json.dumps({'runtime':check(a.config.resolve())}));return 0
        except RuntimeProbeFailed as exc:print(json.dumps({'runtime':'unavailable','error':str(exc)}));return 3
    help_requested=any(flag in extra for flag in ('--help','-h'))
    if not help_requested and a.task!='stop': settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(map(str,config['dependencies']))+os.pathsep+env.get('PYTHONPATH','')
    # Placement in either inbox is the user's decision, independent of the
    # requested scan's --apply flag. Help/status/model tests remain read-only.
    automatic=not help_requested and a.task not in {'triage-batch','compare-selection','review-status','review-reasons','nsfw-model-test','nsfw-policy-test','stop'}
    if a.task=='review-pending' and '--verify' in extra:automatic=False
    explicit_privacy=a.task in {'nsfw-private','nsfw-vault-transfer'} or (a.task=='nsfw-review' and any(flag in extra for flag in ('--setup','--reject-sha256','--deliver')))
    inbox_result=None
    if automatic and explicit_privacy and a.task in {'nsfw-private','nsfw-vault-transfer'}:
        import nsfw_review
        intake=nsfw_review.process(config,apply=True)
        if intake['held']:
            print(json.dumps({'inbox_holds':intake['held']}),flush=True);return 2
    if automatic and not explicit_privacy:
        import nsfw_review
        account_parser=argparse.ArgumentParser(add_help=False)
        account_parser.add_argument('--username')
        account_args,_=account_parser.parse_known_args(extra)
        full_cleanup=a.task in {'cleanup','process-gallery'} and '--folder' not in extra
        # User decision inboxes finish before starting the gallery scan. DELETE
        # performs its own fresh exact-content verification, never waits for filters.
        inbox_config={**config,'defer_review_deletions':False}
        inbox_result=nsfw_review.service(inbox_config,username=account_args.username)
        print(json.dumps({'inboxes':{'restore':inbox_result['restore'],'delete':inbox_result.get('delete',{}),'confirmations':inbox_result['new_confirmations'],
            'sfw':inbox_result.get('decision_folder_transfers',{}).get('sfw',{}),
            'held':inbox_result['held'],'report':str(config['output']/'nsfw-review'/'automatic-inbox-report.json')}}),flush=True)
        if a.task in {'restore','nsfw-review','review-decisions'}:return 2 if inbox_result['held'] else 0
        # Failed RESTORE intake must not permit a subsequent detector/move to
        # race a keep decision. Provider-only holds do not prevent local scans.
        if any('provider' not in hold and not hold.get('nonblocking') for hold in inbox_result['held']):return 2
    if a.task in {'process-gallery','cleanup','process-folder'} and not help_requested and not os.environ.get('PHOTOS_HEALTH_CHECKED'):
        from runtime_health import check,RuntimeProbeFailed
        try:print(json.dumps({'runtime':check(a.config.resolve())}),flush=True)
        except RuntimeProbeFailed as exc:print(json.dumps({'runtime':'unavailable','error':str(exc)}));return 3
    import scan_import
    if automatic and a.task in scan_import.SCAN_TASKS:
        scan_import.run(config)
        env.update({k:v for k,v in os.environ.items() if k.startswith('PHOTOS_')})
    code=subprocess.run(command(a.task,config,extra),env=env).returncode
    if automatic and explicit_privacy and a.task in {'nsfw-private','nsfw-vault-transfer'}:
        import nsfw_review
        inbox_result=nsfw_review.service(config)
        print(json.dumps({'inbox_holds':inbox_result['held']}),flush=True)
    return code or (2 if inbox_result and inbox_result['held'] else 0)

if __name__ == '__main__': raise SystemExit(main())
