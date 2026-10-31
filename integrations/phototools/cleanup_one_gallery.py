"""Fresh single-gallery cleanup using the maintained detectors and review movers."""
import argparse
import json
from pathlib import Path
import photo_settings as settings
import photo_pass
from runtime_health import check
from scan_lifecycle import ScanCancelled, discard_partials
from streaming_cleanup import StreamingCleanup, verify_early
from summarize_photo_pass import summarize
from two_library_duplicates import read_scan
from unattended_cleanup import keep_awake


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gallery', choices=['onedrive', 'icloud'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--workers', type=int, default=0, help='File workers; 0 selects automatically')
    parser.add_argument('--checks', nargs='+', default=['duplicates','live-photos','junk','nsfw','tiktok','quality'], choices=['duplicates','junk','live-photos','nsfw','tiktok','quality'])
    parser.add_argument('--exclude-import', action='store_true')
    parser.add_argument('--resident-only', action='store_true', help='Skip cloud placeholders without downloading originals')
    args = parser.parse_args()
    config = settings.load()
    if args.apply:
        import restore_family
        restore_family.before_run(config)
    check(Path(__file__).with_name('photos.local.json'))
    config = {**config, 'download_originals': not args.resident_only, 'single_gallery_cleanup': True,
              'analysis_scope': 'separate_galleries'}
    import scan_import
    scan_import.run(config, excluded=args.exclude_import)
    checks = args.checks
    import decision_inboxes
    initial_inboxes=decision_inboxes.finish(config) if args.apply else None
    if initial_inboxes and any('provider' not in h and not h.get('nonblocking') for h in initial_inboxes['held']):
        raise ValueError('RESTORE/action inbox holds must resolve before review')
    out = args.output.resolve()
    keep_awake(True)
    try:
        handler = StreamingCleanup(config, out, checks=checks) if args.apply else None
        summary = photo_pass.run(config, checks, [args.gallery], out, workers=args.workers, action_handler=handler)
        rows = read_scan(out / 'files.jsonl')
        verification = verify_early(rows)
        report = summarize(out, config)
        import decision_inboxes
        inboxes = decision_inboxes.finish(config) if args.apply else None
        result = {'status': 'finished_with_review_or_holds', 'gallery': args.gallery,
                  'scan': summary, 'verification': verification, 'review': report,
                  'reconciliation_requested': False}
        result['end_review_decisions'] = inboxes
        (out / 'workflow-summary.json').write_text(json.dumps(result, indent=2))
        (out / 'report.md').write_text(
            f"{args.gallery} cleanup finished.\n\n"
            f"Files checked: {summary.get('processed', 0)}\n\n"
            "See workflow-summary.json for actual moves and pending checks.\n"
            "Review ordinary files in processed_files/FOR_REVIEW and private files in processed_files/NSFW.\n")
        return 0
    except (ScanCancelled, KeyboardInterrupt):
        discard_partials(out)
        return 130
    finally:
        keep_awake(False)


if __name__ == '__main__':
    raise SystemExit(main())
