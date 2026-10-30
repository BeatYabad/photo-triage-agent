"""Resumable read-only semantic image ranking. All results require human review.

No source mutations or uploads. A killable process bounds each file read/decode.
CLIP similarities are uncalibrated ranking scores, never deletion decisions.
"""
import argparse
from collections import Counter
from itertools import zip_longest
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3
import sys
import time
from photo_settings import hashes

VERSION = 'photo-triage-lfm-v1'
PHOTOS = set('.jpg .jpeg .jpe .png .heic .heif .gif .tif .tiff .bmp .webp .avif .dng .raw .cr2 .cr3 .nef .arw .orf .rw2 .raf .pef .srw'.split())
NONRESIDENT = 0x1000 | 0x40000 | 0x400000
SKIP_DIRS = {'restore', 'processed_files', 'for_review', '$recycle.bin', '.dtrash', 'trash'}
PROMPTS = {
 'meme_or_cartoon': ['a funny internet meme with a caption', 'a cartoon or comic strip with text', 'a humorous graphic shared on social media'],
 'presentation_slide': ['a photograph of a projected PowerPoint presentation slide with a title and bullet points', 'a screenshot of a business presentation slide or slide deck', 'a presentation slide with a diagram chart and heading'],
 'receipt': ['a close up photograph of a retail shopping receipt', 'a screenshot of a store purchase receipt'],
 'screenshot': ['a screenshot of a phone application or web page', 'a screenshot of a computer desktop'],
 'store_price_comparison': ['a photo of a store shelf price tag showing the price of a product', 'a shopping reference photo of an item for sale and its price label', 'a photo of a retail sale price sign'],
 'restaurant_menu': ['a close up photo of a restaurant menu with food names and prices', 'a photo of a takeout food menu', 'a photo of a restaurant menu board'],
 'product_packaging_label': ['a close up photo of product packaging and its printed product label', 'a photo of a food package ingredients and nutrition facts label', 'a close up reference photo of an appliance model number sticker'],
 'event_notice_or_pass': ['a photo of a temporary event flyer with a date and time', 'a photo of an admission ticket or event pass', 'a photo of a poster announcing a sale or upcoming event'],
 'computer_screen_or_slides': ['a camera photo of a computer monitor displaying a software window', 'a photo of a projected presentation slide', 'a screenshot of a computer software error message'],
 'repair_reference': ['a close up reference photo of electrical wiring and connectors during a repair', 'a close up photo of a disassembled appliance being repaired', 'a reference photo of a broken mechanical part or plumbing connection'],
 'protect_people_family': ['a family photograph of people posing together', 'a photo of a person, a child, a baby, or a selfie', 'a candid photograph of people at a memorable event'],
 'protect_medical': ['a photo of a medical record, prescription, hospital paperwork, or test results', 'a photo of prescription medication with a patient name', 'a medical photograph documenting an injury or health condition'],
 'protect_legal_financial_identity': ['a photo of a bank statement, check, invoice, receipt, or financial record', 'a photo of a legal document, signed contract, or official correspondence', 'a photo of a passport, driver license, identification card, or tax document'],
 'protect_school': ['a photo of school homework, report card, diploma, or a certificate', 'a photo of a child artwork or handwritten personal note', 'a photo of classroom materials or educational notes'],
 'protect_property': ['a photo documenting damage to a home or property for insurance', 'a photo of a house inspection report, deed, title, or home repair invoice', 'a photograph documenting the condition of a car or personal property'],
 'protect_memorabilia': ['a photo of a cherished souvenir, keepsake, or memorable ticket', 'a photo of an old family photograph or personal letter', 'a photo of a meaningful collectible, artwork, trophy, or award'],
 'other_nature_pets': ['a photograph of a landscape, garden, flowers, or wildlife', 'a photo of a dog, cat, or family pet', 'a scenic travel photograph of a building or city'],
 'other_personal_objects': ['a photograph of food, a meal, or a drink', 'a photo of a hobby project, craft, or handmade object', 'a photograph of a room, vehicle, furniture, or household object'],
 # Meme/comic prompts belong only to the requested meme category. Giving the
 # same concept to a non-target makes genuine memes lose on wording alone.
 'other_graphic': ['a decorative graphic or abstract illustration without a joke', 'a screenshot of a personal conversation or message'],
}
TARGETS = tuple(k for k in PROMPTS if not k.startswith(('protect_', 'other_')))

def utc(): return datetime.now(timezone.utc).isoformat()

def load_exclusions(paths):
    if len(paths)<2:raise ValueError('Both reviewed-wanted ledgers are mandatory')
    from photo_settings import review_hashes
    return hashes(paths) | review_hashes({'reviewing','copy_pending','restore_pending','kept'})

def linked(path):
    return path.is_symlink() or path.is_junction()

def inventory(roots, extensions=None):
    rows, issues = [], []
    for label, root in roots.items():
        if not root.is_dir(): raise ValueError('Missing root: ' + str(root))
        if any(linked(p) for p in (root, *root.parents)): raise ValueError('Linked root')
        def failure(exc): issues.append({'label': label, 'path': exc.filename, 'error': str(exc), 'status': 'inventory_error'})
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=failure):
            clean = []
            for name in dirs:
                p = Path(directory)/name
                if name.casefold() in SKIP_DIRS: continue
                if linked(p):
                    issues.append({'label': label, 'path': str(p), 'status': 'held_linked_directory'})
                else: clean.append(name)
            dirs[:] = clean
            for name in files:
                p = Path(directory)/name
                if p.suffix.lower() not in (extensions or PHOTOS): continue
                try:
                    st = p.lstat()
                    rows.append({'label': label, 'path': str(p), 'bytes': st.st_size, 'mtime_ns': st.st_mtime_ns,
                                 'linked': linked(p)})
                except OSError as exc: failure(exc)
    return rows, issues

def rank(scores):
    target = max(TARGETS, key=lambda k: scores[k])
    protect = max((k for k in scores if k.startswith('protect_')), key=lambda k: scores[k])
    other = max((k for k in scores if k.startswith('other_')), key=lambda k: scores[k])
    gap = scores[target] - max(scores[protect], scores[other])
    candidate = scores[target] >= .245 and gap >= .005
    holds = ['visual_review_required'] if candidate else []
    if candidate and scores[protect] >= scores[target] - .035: holds.append('protected_content_near_target_score')
    if candidate and target == 'event_notice_or_pass': holds.append('expiration_and_memorabilia_value_unverified')
    if candidate and target == 'repair_reference': holds.append('property_record_or_useful_reference_unverified')
    if candidate and target == 'computer_screen_or_slides': holds.append('screen_content_needs_sensitive_record_review')
    return {'category': target if candidate else None, 'nearest_target': target,
            'candidate': candidate, 'confidence_band': ('high_ranking' if candidate and scores[target] >= .29 and gap >= .025 else 'review_ranking') if candidate else 'not_candidate',
            'target_score': round(scores[target], 5), 'protection_category': protect,
            'protection_score': round(scores[protect], 5), 'margin': round(gap, 5),
            'scores': {k: round(v, 5) for k, v in scores.items()}, 'holds': holds}

def interleave_roots(rows):
    groups = {}
    for row in rows: groups.setdefault(row['label'], []).append(row)
    return [row for group in zip_longest(*groups.values()) for row in group if row is not None]

def dependencies(paths):
    for p in reversed(paths): sys.path.insert(0, str(p))

def session(path):
    if Path(path).name=='vision_model_quantized.onnx':
        from inference_backend import ClipSession,activate,profile
        return ClipSession(path,activate(profile()))
    import onnxruntime as ort
    opts = ort.SessionOptions(); opts.intra_op_num_threads = 1; opts.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), sess_options=opts, providers=['CPUExecutionProvider'])

def prepare_text(model, deps):
    from prepare_reference_model import verify_model
    verify_model(model)
    dependencies(deps)
    import numpy as np
    from tokenizers import Tokenizer
    tokenizer = Tokenizer.from_file(str(model/'tokenizer.json'))
    tokenizer.enable_padding(length=77, pad_id=49407, pad_token='<|endoftext|>')
    tokenizer.enable_truncation(max_length=77)
    texts = [s for values in PROMPTS.values() for s in values]
    encoded = tokenizer.encode_batch(texts)
    encoder = session(model/'text_model_quantized.onnx')
    inputs = {'input_ids': np.array([e.ids for e in encoded], dtype=np.int64),
              'attention_mask': np.array([e.attention_mask for e in encoded], dtype=np.int64)}
    output_names = [o.name for o in encoder.get_outputs()]
    output = encoder.run(None, {i.name: inputs[i.name] for i in encoder.get_inputs()})
    emb = output[output_names.index('text_embeds')]
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    np.save(model/'reference-text-embeddings.npy', emb)
    fingerprint = hashlib.sha256(json.dumps(PROMPTS, sort_keys=True).encode())
    for p in [model/'vision_model_quantized.onnx', model/'text_model_quantized.onnx', model/'tokenizer.json']:
        with p.open('rb') as f: fingerprint.update(hashlib.file_digest(f, 'sha256').digest())
    return VERSION + '-' + fingerprint.hexdigest()[:24]

def pixels(image):
    import numpy as np
    from PIL import Image, ImageOps
    # Standard CLIP center crop plus full-image view to retain signs near edges.
    w, h = image.size; ratio = 224/min(w, h)
    resized = image.resize((int(w*ratio), int(h*ratio)), Image.Resampling.BICUBIC)
    x, y = (resized.width-224)//2, (resized.height-224)//2
    center = resized.crop((x, y, x+224, y+224))
    full = ImageOps.pad(image, (224,224), method=Image.Resampling.BICUBIC, color=(123,117,104))
    arr = np.stack([np.asarray(center), np.asarray(full)]).astype(np.float32)/255
    arr = (arr - np.array([.48145466,.4578275,.40821073], np.float32)) / np.array([.26862954,.26130258,.27577711],np.float32)
    return arr.transpose(0,3,1,2)

def analyze_image(image, encoder, embeddings, dimensions, frames=1, make='', model=''):
    """CLIP ranking from a decoded image shared with other local checks."""
    from triage_adapter import classify
    return classify(image, dimensions, frames)


def inspect(row, encoder, embeddings, exclusions, cache, rule):
    import numpy as np
    from PIL import Image, ImageOps
    p = Path(row['path'])
    if row.get('linked'): return {**row, 'status': 'held_linked_file'}
    before = p.stat()
    if getattr(before, 'st_file_attributes', 0) & NONRESIDENT:
        return {**row, 'status': 'held_nonresident_placeholder', 'file_attributes': before.st_file_attributes,
                'holds': ['file_content_not_locally_resident_no_cloud_hydration_attempted']}
    if (before.st_size,before.st_mtime_ns) != (row['bytes'],row['mtime_ns']):
        return {**row, 'status': 'held_changed_since_inventory'}
    with p.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    row = {**row, 'sha256': digest}
    if digest in exclusions: return {**row, 'status': 'held_user_exclusion', 'holds': ['user_reviewed_keep_hash']}
    old = cache.execute('SELECT result FROM hashes WHERE sha256=? AND rule=?', (digest,rule)).fetchone()
    if old:
        result = {**row, **json.loads(old[0]), 'cache_reused': True}
    else:
        with Image.open(p) as source:
            dimensions = list(source.size); frames = getattr(source, 'n_frames', 1)
            exif = source.getexif(); make = str(exif.get(271, '')); model = str(exif.get(272, ''))
            source.draft('RGB',(1024,1024))
            image = ImageOps.exif_transpose(source).convert('RGB')
            result = {**row, **analyze_image(image,encoder,embeddings,dimensions,frames,make,model),
                      'cache_reused':False}
    after = p.stat()
    if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
        return {**row, 'status': 'held_changed_during_read'}
    return result

def worker(conn, model, deps, exclusions, database, rule, analysis='reference',nsfw_backend='marqo',nsfw_threshold=.5):
    try:
        known_private=set()
        if analysis=='nsfw' and os.environ.get('PHOTOS_CONFIG'):
            import photo_settings
            from privacy_routes import hashes as private_hashes
            known_private=private_hashes(photo_settings.load())
        dependencies(deps)
        import numpy as np
        import pillow_heif
        pillow_heif.register_heif_opener()
        if analysis=='reference':
            encoder = embeddings = None
        elif analysis=='nsfw':
            import nsfw_analysis
            detector=nsfw_analysis.create_detector(model,nsfw_backend,nsfw_threshold)
        else:
            import quality_analysis
            detector=model
        cache = sqlite3.connect('file:' + str(database).replace('\\','/') + '?mode=ro', uri=True)
        conn.send({'ready': True})
        while True:
            row = conn.recv()
            if row is None: break
            try:
                if analysis=='reference':result=inspect(row,encoder,embeddings,exclusions,cache,rule)
                elif analysis=='nsfw':result=nsfw_analysis.inspect(row,detector,exclusions,cache,rule,known_private=known_private)
                else:result=quality_analysis.inspect(row,detector,exclusions,cache,rule)
            except Exception as exc: result = {**row, 'status': 'error', 'error': type(exc).__name__ + ': ' + str(exc)}
            conn.send(result)
    except BaseException as exc:
        try: conn.send({'worker_error': type(exc).__name__ + ': ' + str(exc)})
        except Exception: pass
    finally: conn.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', type=Path)
    parser.add_argument('--dependencies', type=Path, action='append', default=[])
    parser.add_argument('--exclusions', type=Path, action='append', required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--file-timeout', type=float, default=45)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--analysis',choices=['reference','nsfw','quality'],default='reference')
    parser.add_argument('--nsfw-backend',choices=['marqo','nudenet'],default='marqo')
    parser.add_argument('--nsfw-threshold',type=float,default=.5)
    parser.add_argument('--blur-threshold',type=float,default=25)
    parser.add_argument('--detail-threshold',type=float,default=45)
    parser.add_argument('--min-long-edge',type=int,default=640)
    args = parser.parse_args()
    if not 1 <= args.workers <= 6 or args.file_timeout <= 0 or args.limit<0: parser.error('Invalid worker count, timeout or limit')
    if args.analysis!='quality' and args.model is None:parser.error('--model is required for the selected local model')
    if not 0<args.blur_threshold<10000 or not 0<args.detail_threshold<10000 or args.min_long_edge<1:parser.error('Invalid quality thresholds')
    roots = {k:Path(v).resolve() for k,v in (r.split('=',1) for r in args.root)}
    out = args.output.resolve()
    if any(out == r or r in out.parents for r in roots.values()): raise ValueError('Output inside source')
    out.mkdir(parents=True,exist_ok=True)
    exclusions = load_exclusions(args.exclusions)
    lock = out/'scan.lock'
    with lock.open('x') as f: f.write(str(os.getpid()))
    slots = []; db = None; started = time.monotonic(); run = str(time.time_ns())
    try:
        import nsfw_analysis
        rows, issues = inventory(roots,PHOTOS|nsfw_analysis.VIDEOS if args.analysis=='nsfw' else PHOTOS)
        with (out/'inventory.jsonl').open('w',encoding='utf-8') as f:
            for row in rows: f.write(json.dumps(row)+'\n')
        (out/'inventory-issues.json').write_text(json.dumps(issues,indent=2),encoding='utf-8')
        rows = interleave_roots(rows)
        if args.limit: rows = rows[:args.limit]
        print(json.dumps({'stage':'inventory','photos':len(rows),'counts':dict(Counter(r['label'] for r in rows)), 'issues':len(issues)}),flush=True)
        dependencies(args.dependencies)
        import quality_analysis
        quality_options={'blur_threshold':args.blur_threshold,'detail_threshold':args.detail_threshold,'min_long_edge':args.min_long_edge}
        model=quality_options if args.analysis=='quality' else args.model.resolve()
        rule = (('photo-triage-'+__import__('triage_adapter').fingerprint()) if args.analysis=='reference' else
                nsfw_analysis.rule(model,args.nsfw_backend,args.nsfw_threshold) if args.analysis=='nsfw' else quality_analysis.rule(quality_options))
        database = out/'semantic-cache.sqlite'; db = sqlite3.connect(database)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS hashes(sha256 TEXT,rule TEXT,result TEXT,PRIMARY KEY(sha256,rule))'); db.commit()
        done = 0; counts = Counter(); labels = Counter(); candidates = 0; cached = 0; partial_frames = 0
        pending = iter(rows); ctx = mp.get_context('spawn')
        def launch():
            parent, child = ctx.Pipe()
            process = ctx.Process(target=worker,args=(child,model,args.dependencies,exclusions,database,rule,args.analysis,args.nsfw_backend,args.nsfw_threshold),daemon=True)
            process.start(); child.close()
            return {'process':process,'conn':parent,'row':None,'ready':False,'start':time.monotonic()}
        def summary(status='running'):
            elapsed = time.monotonic()-started
            payload = {'status':status,'run_id':run,'rule':rule,'updated_utc':utc(),'total':len(rows),'processed':done,
                       'candidates':candidates,'hash_cache_reused':cached,'status_counts':dict(counts),'processed_by_root':dict(labels),
                       'elapsed_seconds':round(elapsed,1),'estimated_remaining_seconds':round((len(rows)-done)*elapsed/done) if done else None,
                       'inventory_issues':len(issues),'sample_limit':args.limit,'source_mutations':0,'cloud_coverage_verified':False,
                       'partial_animation_files':partial_frames,
                       'analysis':args.analysis,
                       'limitations':['Quality metrics and model results require visual review','Animated images use first frame only',
                                       'Videos use one-second sampling; unsampled frames and audio are not screened' if args.analysis=='nsfw' else 'Videos are outside reference image scan']}
            tmp=out/'progress.tmp'; tmp.write_text(json.dumps(payload,indent=2),encoding='utf-8'); tmp.replace(out/'progress.json')
            return payload
        with (out/'coverage.jsonl').open('a',encoding='utf-8',buffering=1) as coverage, (out/'candidates.jsonl').open('a',encoding='utf-8',buffering=1) as candidates_file:
            def consume(result):
                nonlocal done,candidates,cached,partial_frames
                result.update({'rule':rule,'run_id':run,'observed_utc':utc()})
                coverage.write(json.dumps(result)+'\n'); done+=1; counts[result['status']]+=1; labels[result['label']]+=1
                if result.get('candidate'):
                    candidates_file.write(json.dumps(result)+'\n'); candidates+=1
                cached += bool(result.get('cache_reused'))
                partial_frames += bool(result.get('frames',1)>1)
                if result['status']=='analyzed' and not result.get('cache_reused'):
                    value={k:v for k,v in result.items() if k not in {'path','label','bytes','mtime_ns','linked','sha256','run_id','observed_utc','rule'}}
                    db.execute('INSERT OR REPLACE INTO hashes VALUES(?,?,?)',(result['sha256'],rule,json.dumps(value))); db.commit()
                if done % 100 == 0:
                    print(json.dumps(summary()),flush=True)
            slots.extend(launch() for _ in range(min(args.workers,len(rows))))
            summary()
            last_report=time.monotonic()
            while slots:
                for slot in slots[:]:
                    process,conn=slot['process'],slot['conn']
                    if conn.poll():
                        try: result=conn.recv()
                        except EOFError: result={'worker_error':'worker exited without response'}
                        if 'worker_error' in result:
                            if slot['row']: consume({**slot['row'],'status':'error','error':result['worker_error']})
                            else: raise RuntimeError(result['worker_error'])
                            slot['row']=None; process.terminate(); process.join(2); conn.close(); slots.remove(slot); slots.append(launch()); continue
                        if result.get('ready'): slot['ready']=True
                        else: consume(result); slot['row']=None
                    if slot['ready'] and slot['row'] is None:
                        row=next(pending,None)
                        if row is None:
                            conn.send(None); process.join(3); conn.close(); slots.remove(slot); continue
                        conn.send(row); slot['row']=row; slot['start']=time.monotonic()
                    timeout=args.file_timeout if slot['ready'] else 180
                    if time.monotonic()-slot['start'] > timeout:
                        process.terminate(); process.join(3); conn.close(); slots.remove(slot)
                        if slot['row']: consume({**slot['row'],'status':'held_timeout','error':f'Per-file deadline {timeout}s exceeded; no retry or mutation'})
                        else: raise RuntimeError('Worker initialization timeout')
                        slots.append(launch())
                if time.monotonic()-last_report>15: summary(); last_report=time.monotonic()
                time.sleep(.05)
            status='sample_complete' if args.limit else ('complete_with_holds' if issues or partial_frames or any(k!='analyzed' for k in counts) else 'complete')
            print(json.dumps(summary(status)),flush=True)
            if args.analysis=='nsfw':
                coverage.flush()
                from sexual_content import write_uncertain
                with (out/'coverage.jsonl').open(encoding='utf-8') as report_source:
                    write_uncertain(out,(json.loads(s) for s in report_source if s.strip()))
            return 2 if status!='complete' else 0
    finally:
        for slot in slots:
            slot['process'].terminate(); slot['process'].join(2); slot['conn'].close()
        if db: db.close()
        lock.unlink(missing_ok=True)

if __name__ == '__main__': raise SystemExit(main())
