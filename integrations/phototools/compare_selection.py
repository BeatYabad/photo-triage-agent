"""Read-only, explicit-file Czkawka comparisons; temporary images need opt-in.

Original paths are never passed to Czkawka. Decoders preserve the original bytes;
temporary comparison PNGs are removed on success and failure. No inbox actions.
"""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import czkawka_backend


def fingerprint(path):
    before = path.stat()
    # Do not hydrate offline or recall-on-access provider placeholders for a test.
    if getattr(before, 'st_file_attributes', 0) & (0x1000 | 0x40000 | 0x400000):
        raise ValueError('Unavailable provider bytes; source left untouched')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Source changed while reading')
    return {'sha256': digest, 'bytes': after.st_size, 'mtime_ns': after.st_mtime_ns}


def prepare(path, png, exact):
    from visual_duplicates import image
    identity = fingerprint(path)
    im, quality = image(path)
    pixel_hash = hashlib.sha256(str(im.size).encode() + im.tobytes()
                                + im.info.get('icc_profile', b'')).hexdigest()
    dimensions = im.size
    im.thumbnail((1280,1280))
    im.save(png, format='PNG')
    import shutil
    shutil.copyfile(path, exact)
    if fingerprint(exact)['sha256'] != identity['sha256']:
        raise ValueError('Temporary exact comparison copy failed verification')
    if fingerprint(path) != identity:
        raise ValueError('Source changed during decoding')
    return {**identity, 'dimensions': dimensions, 'pixel_sha256': pixel_hash,
            'quality': quality}


def compare(rows, *, allow_temporary_images=False, difference=10):
    if not allow_temporary_images:
        raise ValueError('Temporary comparison images require explicit authorization')
    if not rows or len({str(Path(r['path']).resolve()).casefold() for r in rows}) != len(rows):
        raise ValueError('Selection must contain distinct explicit paths')
    czkawka_backend.verify_executable()
    report = {'engine':'czkawka','version':czkawka_backend.VERSION,'files':[],
              'similar_groups':[], 'pixel_groups':[], 'exact_groups':[], 'holds':[],
              'actions':0,'temporary_images_removed':False}
    # OS temporary storage, never the project or a gallery.
    with tempfile.TemporaryDirectory(prefix='phototools-czkawka-') as temp:
        root=Path(temp).resolve();staged={};exact_staged={};libraries={}
        if any(root == Path(r['path']).parent or root in Path(r['path']).parents for r in rows):
            raise ValueError('Comparison workspace overlaps an original')
        for number,row in enumerate(rows):
            source=Path(row['path']).resolve(strict=True)
            library=str(row.get('library','selection'))
            if library not in libraries:
                libraries[library]=root/str(len(libraries));libraries[library].mkdir()
                (root/('exact-'+libraries[library].name)).mkdir()
            png=libraries[library]/f'{number}.png'
            exact=root/('exact-'+libraries[library].name)/f'{number}.bin'
            try:
                bootstrap='import sys,runpy; sys.path='+repr(sys.path)+'; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")'
                completed=subprocess.run([getattr(sys,'_base_executable',sys.executable),'-c',bootstrap,
                    str(Path(__file__).resolve()),'--prepare',str(source),str(png),str(exact)],
                    capture_output=True,text=True,timeout=300,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if completed.returncode:
                    raise ValueError(completed.stderr[-1200:])
                data=json.loads(completed.stdout)
                if row.get('sha256') and (row['sha256'],row['bytes']) != (data['sha256'],data['bytes']):
                    raise ValueError('Selection bytes changed')
                item={**row,**data,'path':str(source),'library':library}
                staged[str(png).casefold()]=item;report['files'].append(item)
                exact_staged[str(exact).casefold()]=item
            except Exception as exc:
                png.unlink(missing_ok=True)
                exact.unlink(missing_ok=True)
                report['holds'].append({'path':str(source),'error':str(exc)})
        for library,folder in libraries.items():
            if len(list(folder.iterdir())) < 2:continue
            found=czkawka_backend.scan('similar',[folder],root/(folder.name+'.json'),difference=difference)
            for group in found['groups']:
                report['similar_groups'].append({'library':library,'members':[
                    {'path':staged[str(Path(e['path']).resolve()).casefold()]['path'],
                     'difference':e['difference']} for e in group],
                    'review_required':True,'deletion_authorized':False})
            exact_found=czkawka_backend.scan('exact',[root/('exact-'+folder.name)],root/('exact-'+folder.name+'.json'))
            for group in exact_found['groups']:
                members=[exact_staged[str(Path(e['path']).resolve()).casefold()] for e in group]
                if len({(e['sha256'],e['bytes']) for e in members}) != 1:
                    raise ValueError('Czkawka exact result failed independent SHA-256/length verification')
                report['exact_groups'].append({'library':library,'paths':[e['path'] for e in members]})
        for key,output in [('pixel_sha256','pixel_groups')]:
            groups=defaultdict(list)
            for item in report['files']:
                groups[(item['library'],item[key])].append(item['path'])
            report[output]=[{'library':library,'paths':paths}
                            for (library,_),paths in groups.items() if len(paths)>1]
        for item in report['files']:
            if fingerprint(Path(item['path'])) != {k:item[k] for k in ('sha256','bytes','mtime_ns')}:
                raise ValueError('Original changed during comparison')
    report['temporary_images_removed']=not root.exists()
    report['status']='completed_with_holds' if report['holds'] else 'completed'
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--allow-temporary-images',action='store_true')
    parser.add_argument('--difference',type=int,default=10)
    parser.add_argument('--prepare',nargs=3,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.prepare:
        print(json.dumps(prepare(*map(Path,args.prepare))));return 0
    if not args.selection or not args.output:parser.error('--selection and --output required')
    if args.output.exists():raise FileExistsError('Use a new report path')
    rows=json.loads(args.selection.read_text(encoding='utf-8-sig'))
    result=compare(rows,allow_temporary_images=args.allow_temporary_images,difference=args.difference)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['status'],'files':len(result['files']),
                      'similar_groups':len(result['similar_groups']),'holds':len(result['holds'])}))
    return 2 if result['holds'] else 0


if __name__=='__main__':raise SystemExit(main())
