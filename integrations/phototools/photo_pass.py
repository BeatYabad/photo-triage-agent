"""Crawl selected galleries once and run every requested check on each file.

Read-only unless an explicit action coordinator is supplied by an applying workflow.
Local models stay loaded; exact content shares checks within the fresh run.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import photo_settings as settings
import scan_reference_photos as scan
import audit_two_libraries as audit
import merge_photos as media
from photo_pass_analysis import Analyzer, IMAGE_CHECKS, failure

CHECKS=('duplicates','sync','live-photos','quality','junk','nsfw','tiktok')

def write(path, data):
    from two_library_duplicates import write_json
    write_json(path,data)

def write_progress(path,data):
    """A failed status snapshot must not abort analysis or action receipts."""
    try:
        write(path,data)
        return True
    except OSError as exc:
        print(json.dumps({'stage':'progress_write_failed','error':str(exc),
                          'processed':data.get('processed'),'scan_continues':True}),flush=True)
        return False

def inventory(roots, mappings, excluded=(), include_unknown=False):
    if len(roots)<2:return _inventory(roots,mappings,excluded,include_unknown)
    with ThreadPoolExecutor(max_workers=min(2,len(roots))) as pool:
        futures=[pool.submit(_inventory,{label:root},mappings,excluded,include_unknown) for label,root in roots.items()]
        results=[future.result() for future in futures]
    return [row for rows,_ in results for row in rows],[issue for _,issues in results for issue in issues]


def _inventory(roots, mappings, excluded=(), include_unknown=False):
    """Only root traversal in this workflow; sidecars enter the same inventory."""
    rows=[];issues=[]
    from nsfw_analysis import VIDEOS
    extensions=scan.PHOTOS|VIDEOS|set(media.SIDECARS)
    skip=scan.SKIP_DIRS|audit.SKIP|{'shared'}
    for label,root in roots.items():
        if not root.is_dir() or any(scan.linked(p) for p in (root,*root.parents)):
            raise ValueError('Missing or linked gallery: '+str(root))
        if any(part.casefold() in skip for part in root.parts):raise ValueError('Excluded gallery root')
        def traversal_error(error):
            path=Path(error.filename) if error.filename else None
            # Sync/sorting can remove a child after walk enumerates its parent.
            # Only a freshly absent descendant is harmless; inaccessible trees
            # and a disappearing gallery root still leave an inventory gap.
            if isinstance(error,FileNotFoundError) and path is not None and path!=root and path.is_relative_to(root):
                try:
                    path.stat()
                except FileNotFoundError:
                    return
                except OSError:
                    pass
            issues.append({'path':str(path) if path is not None else str(root),
                           'error':str(error),'traversal_gap':True})
        for directory,dirs,files in os.walk(root,followlinks=False,onerror=traversal_error):
            for name in list(dirs):
                p=Path(directory)/name
                if label=='import' and Path(directory)==root and name.casefold()=='pending':
                    dirs.remove(name);continue
                if name.casefold() in skip or scan.linked(p) or p.resolve() in excluded:dirs.remove(name)
            for name in files:
                p=Path(directory)/name;ext=p.suffix.lower()
                if ext not in extensions and not include_unknown:continue
                try:
                    media.inside(p,root);st=p.stat()
                    records=mappings.get(str(p).casefold(),[]) if label=='icloud' else []
                    status='inventoried'
                    if records and all(any(r[k] for k in ('is_deleted','is_deleted_locally','is_expunged')) for r in records):status='excluded_deleted_asset'
                    elif records and not any(r['asset_key']=='resOriginalRes' for r in records):status='excluded_cached_rendition'
                    rows.append({'library':label,'label':label,'root':str(root),'path':str(p),
                        'kind':'video' if ext in VIDEOS else 'photo' if ext in scan.PHOTOS else 'sidecar' if ext in media.SIDECARS else 'unsupported',
                        'bytes':st.st_size,'mtime_ns':st.st_mtime_ns,'status':status,
                        'placeholder':bool(getattr(st,'st_file_attributes',0)&scan.NONRESIDENT),
                        'native_live_photo':any(r.get('compl_size',0)>0 and r['dataclass_type']==1 for r in records)})
                except (OSError,ValueError) as exc:issues.append({'path':str(p),'error':str(exc)})
    return rows,issues

def stable(row):
    p=Path(row['path']);media.inside(p,Path(row['root']));st=p.stat()
    if getattr(st,'st_file_attributes',0)&scan.NONRESIDENT:raise ValueError('Nonresident file; no hydration attempted')
    if (st.st_size,st.st_mtime_ns)!=(row['bytes'],row['mtime_ns']):raise ValueError('File changed during pass')
    return p

def fingerprint(row):
    path=stable(row)
    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    stable(row)
    return digest


def download_original(row):
    """Called inside a bounded worker; fresh full hashing still follows hydration."""
    import shutil
    from cloud_photo_bytes import hydrate
    path=Path(row['path']);media.inside(path,Path(row['root']));before=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(row['bytes'],row['mtime_ns']):
        raise ValueError('Cloud original changed since the fresh inventory')
    if getattr(before,'st_file_attributes',0)&scan.NONRESIDENT:
        if shutil.disk_usage(path.parent).free < before.st_size+50*1024**3:
            raise ValueError('Keep 50 GiB free while downloading originals')
        hydrate(path)
    after=path.stat()
    if after.st_size!=before.st_size or getattr(after,'st_file_attributes',0)&scan.NONRESIDENT:
        raise ValueError('Full cloud original is not available after the download')
    return {**row,'bytes':after.st_size,'mtime_ns':after.st_mtime_ns,'placeholder':False,
            'cloud_download_verified':False}  # Only the subsequent full hash verifies bytes.

def release_claim(path):
    """Windows readers can briefly deny unlink; never abandon an owned lease."""
    for delay in (0,.01,.02,.04,.08,.16,.32):
        if delay:time.sleep(delay)
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if delay==.32:raise


def reclaim_dead_claim(path):
    from photo_pass_resume import process_alive
    gate=path.with_suffix('.reclaim')
    try:lock=gate.open('x')
    except FileExistsError:return False
    try:
        with lock:
            recorded=path.read_bytes();owner=json.loads(recorded).get('pid')
            if isinstance(owner,int) and owner>0 and not process_alive(owner) and path.read_bytes()==recorded:
                path.unlink();return True
    except FileNotFoundError:return True
    except (OSError,ValueError):return False
    finally:release_claim(gate)
    return False


class FileProcessor:
    """One hash per path, shared decode, successful-result cache per exact identity."""
    def __init__(self, config, checks, database, protected, deleted, analyzer=None):
        self.config=config;self.checks=set(checks);self.protected=protected;self.deleted=deleted
        self.private=set()
        if 'nsfw' in self.checks and config.get('review_state'):
            from privacy_routes import hashes as private_hashes
            self.private=private_hashes(config)-settings.hashes(config['safe_ledgers'])
        self.analyzer=analyzer or Analyzer(config)
        self.database=Path(database)
        self.claims=self.database.with_suffix('.claims');self.claims.mkdir(exist_ok=True)
        self.db=sqlite3.connect(database,timeout=30)
        self.db.execute('CREATE TABLE IF NOT EXISTS results(sha TEXT,bytes INTEGER,kind TEXT,check_name TEXT,rule TEXT,result TEXT,PRIMARY KEY(sha,bytes,kind,check_name,rule))')
        self.rules={};self.rule_errors={}

    def close(self):
        self.db.close()
        if hasattr(self.analyzer,'close'):self.analyzer.close()

    def claim(self,row):
        """Serialize inference only for the SAME exact content, across workers."""
        path=self.claims/(row['sha256']+'-'+str(row['bytes'])+'-'+row['kind']+'.json')
        end=time.monotonic()+self.config.get('pass_file_timeout',120)
        while True:
            try:
                with path.open('x',encoding='utf-8') as stream:json.dump({'pid':os.getpid()},stream)
                return path
            except FileExistsError:
                # A terminated worker cannot release its exact-content lease.
                # Reclaim only unchanged leases whose recorded owner has exited.
                if reclaim_dead_claim(path):continue
                if time.monotonic()>=end:raise TimeoutError('Exact-content worker still busy; no concurrent inference')
                time.sleep(.1)

    def get(self, row, check):
        if check not in self.rules and check not in self.rule_errors:
            try:self.rules[check]=self.analyzer.rule(check)
            except Exception as exc:self.rule_errors[check]=failure(exc)
        if check in self.rule_errors:return {**self.rule_errors[check],'cache_reused':False}
        found=self.db.execute('SELECT result FROM results WHERE sha=? AND bytes=? AND kind=? AND check_name=? AND rule=?',
            (row['sha256'],row['bytes'],row['kind'],check,self.rules[check])).fetchone()
        return {**json.loads(found[0]),'cache_reused':True} if found else None

    def process(self, row, prepared=False):
        results={};row=dict(row);claim=None
        resumed=row.pop('_resume_checks',{}) if prepared else {}
        import family_faces
        family_revision=family_faces.revision(self.config)
        if family_revision!=getattr(self,'family_revision',None):
            self.family_revision=family_revision
            self.rules.clear();self.rule_errors.clear()
            if hasattr(self.analyzer,'versions'):self.analyzer.versions.clear()
            resumed={}
        if row['kind']=='unsupported':
            return {**row,'status':'unsupported','checks':{check:{'status':'unsupported','candidate':False} for check in self.checks}}
        requested=self.checks & IMAGE_CHECKS
        metadata_needed=bool(self.checks & {'live-photos','sync','duplicates'}) or bool(self.config.get('import_mode') or self.config.get('require_media_metadata') or row.get('_prepared_metadata'))
        if row['placeholder'] and not row['status'].startswith('excluded_') and self.config.get('download_originals'):
            try:row=download_original(row)
            except Exception as exc:
                return {**row,'status':'cloud_download_failed','cloud_download_attempted':True,
                    'checks':{check:failure(exc) for check in self.checks}}
        if row['status'].startswith('excluded_') or row['placeholder']:
            status=row['status'] if row['status'].startswith('excluded_') else 'placeholder_unverified'
            return {**row,'status':status,'checks':{check:{'status':status,'candidate':False} for check in self.checks}}
        try:
            if prepared:stable(row)
            else:row['sha256']=fingerprint(row)
            row['status']='verified_local_bytes'
            if 'cloud_download_verified' in row:row['cloud_download_verified']=True
            row['hash_evidence']='full_scan_hash_unchanged_size_mtime'
            if 'nsfw' in self.checks:
                if row['sha256'] in self.private:
                    checks={check:{'status':'held_private_route','candidate':False} for check in self.checks}
                    checks['nsfw']={'status':'analyzed','candidate':True,'category':'nsfw',
                        'rule':'unified-pass-v1-private-route','analysis_check':'nsfw','cache_reused':True,
                        'reason':'Known private identity: route returned copies; do not re-screen'}
                    return {**row,'checks':checks,'metadata_hold':'known_private_content_not_rescreened'}
            if row['sha256'] in self.protected or row['sha256'] in self.deleted:
                status='held_user_exclusion' if row['sha256'] in self.protected else 'held_intentional_deletion'
                return {**row,'metadata_hold':'protected_content_not_screened',
                        'checks':{check:{'status':status,'candidate':False} for check in self.checks}}
            claim=self.claim(row)
            if row['kind']=='sidecar':requested=set()
            needed=requested|({'metadata'} if metadata_needed else set())
            missing=set()
            for check in needed:
                cached=resumed.get(check) or self.get(row,check)
                if cached is None:missing.add(check)
                else:results[check]=cached
            if 'metadata' in missing:
                try:results['metadata']=row.pop('_prepared_metadata') if '_prepared_metadata' in row else self.analyzer.metadata(Path(row['path']))
                except Exception as exc:results['metadata']=failure(exc)
            image_missing=missing & IMAGE_CHECKS
            if image_missing:
                self.analyzer.current_identity={'sha256':row['sha256'],'bytes':row['bytes']}
                method=self.analyzer.video if row['kind']=='video' else self.analyzer.image
                try:results.update(method(Path(row['path']),image_missing))
                except Exception as exc:results.update({check:failure(exc) for check in image_missing})
            stable(row)
            for check in missing:
                result=results.setdefault(check,failure(ValueError('Check omitted its result')))
                result['cache_reused']=False
                # Gaps/errors are retried next run; successful classification is reused.
                if result['status'] in {'analyzed','not_applicable','cataloged'}:
                    stored={k:v for k,v in result.items() if k!='cache_reused'}
                    self.db.execute('INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?)',
                        (row['sha256'],row['bytes'],row['kind'],check,self.rules[check],json.dumps(stored)))
            self.db.commit()
            for check,result in results.items():
                if check in self.rules and not result.get('resume_source'):result.update(rule=self.rules[check],analysis_check=check)
            row.pop('_prepared_metadata',None)
            meta=results.pop('metadata',None)
            if meta is not None:
                row['metadata']=meta;row['content_identifier']=meta.get('content_identifier')
                if self.checks & {'sync','duplicates','live-photos'}:
                    # LivePhotoVideoIndex alone is not an association. Native
                    # status comes from the client DB; embedded IDs are separate.
                    row['metadata_hold']=None if meta['status']=='cataloged' else 'association_metadata_unavailable'
                if 'live-photos' in self.checks:
                    results['live-photos']={'status':meta['status'],'content_identifier':meta.get('content_identifier'),
                                           'holds':meta.get('holds',[]),'candidate':False}
            for check in self.checks:
                if check in {'duplicates','sync'}:results[check]={'status':'included_in_inventory_plan','candidate':False}
                elif check not in results:results[check]={'status':'not_applicable','candidate':False}
            if results.get('nsfw',{}).get('status')=='needs_review':
                row['metadata_hold']=row.get('metadata_hold') or 'sexual_content_unconfirmed'
            return {**row,'checks':results}
        except Exception as exc:
            row.pop('sha256',None)
            return {**row,'status':'unreadable','error':str(exc),
                    'checks':{check:failure(exc) for check in self.checks}}

        finally:
            if claim:release_claim(claim)

def worker(conn, config, checks, database, protected, deleted):
    processor=None
    try:
        from photo_runtime import configure
        configure(config)
        if config.get('pipeline_stage')!='prepare':
            import cv2
            cv2.setNumThreads(1)
        for dep in reversed(config.get('dependencies',[])):
            import sys
            sys.path.insert(0,str(dep))
        if config.get('pipeline_stage')=='prepare':
            from photo_pipeline import Preparation
            processor=Preparation(config,checks)
        else:processor=FileProcessor(config,checks,database,protected,deleted)
        conn.send({'ready':True})
        while True:
            row=conn.recv()
            if row is None:break
            before=dict(getattr(getattr(processor,'analyzer',None),'timings',{}));started=time.perf_counter()
            result=processor.process(row) if config.get('pipeline_stage')=='prepare' else processor.process(row,prepared=config.get('pipeline_stage')=='inference')
            if config.get('pipeline_stage')!='prepare':
                timings=dict(row.get('timings',{}));timings['analysis_total']=time.perf_counter()-started
                for name,value in processor.analyzer.timings.items():timings[name]=value-before.get(name,0)
                result['timings']=timings
                result.pop('_prepared_metadata',None)
            conn.send(result)
    except BaseException as exc:
        try:conn.send({'worker_error':str(exc)})
        except (OSError,EOFError):pass
    finally:
        if processor:processor.close()
        conn.close()

class RuntimeUnavailable(RuntimeError):
    """Worker infrastructure failed; do not label subsequent media unreadable."""

class BoundedProcessor:
    def __init__(self, config, checks, database, protected, deleted, timeout):
        self.args=(config,checks,database,protected,deleted);self.timeout=min(float(timeout),300)
        self.process=None;self.conn=None
        self.lifecycle=threading.RLock();self.stopping=threading.Event();self.restarts=0
        self.consecutive_failures=0
    def close(self):
        with self.lifecycle:
            process,conn=self.process,self.conn;self.process=None;self.conn=None
        if process:
            if process.is_alive():
                if os.name=='nt':
                    import subprocess
                    try:
                        subprocess.run(['taskkill.exe','/PID',str(process.pid),'/T','/F'],
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    except (OSError,subprocess.TimeoutExpired):
                        # A slow taskkill must not abort the gallery or strand
                        # its process handle. Terminate the owned worker directly.
                        if process.is_alive():process.terminate()
                else:process.terminate()
            process.join(5)
            if process.is_alive():process.kill();process.join(5)
            pid=process.pid
            conn.close()
            # Clear only the private leases belonging to our terminated worker.
            folder=Path(self.args[2]).with_suffix('.claims')
            if folder.exists():
                for path in folder.glob('*.json'):
                    try:
                        if json.loads(path.read_text()).get('pid')==pid:path.unlink()
                    except (OSError,ValueError):pass
    def stop(self):
        self.stopping.set();self.close()
    def launch(self):
        with self.lifecycle:
            if self.stopping.is_set():raise ValueError('Worker stopped')
            ctx=mp.get_context('spawn');parent,child=ctx.Pipe()
            self.process=ctx.Process(target=worker,args=(child,*self.args));self.process.start();child.close();self.conn=parent
        from file_deadline import exchange
        try:value=exchange(parent,min(45,self.timeout))
        except TimeoutError:self.close();raise TimeoutError('Worker startup timeout')
        if not value.get('ready'):self.close();raise ValueError(value.get('worker_error','Worker unavailable'))
    def process_row(self,row):
        ready=False
        started=time.monotonic()
        spent=float(row.get('_file_processing_seconds',0))
        deadline=started+max(0,self.timeout-spent)
        try:
            if self.stopping.is_set():raise ValueError('Worker stopped')
            if not self.process:
                for attempt in range(3):
                    try:self.launch();break
                    except (OSError,EOFError,ValueError,TimeoutError) as exc:
                        self.close();self.restarts+=1
                        if attempt==2:raise RuntimeUnavailable('Worker startup failed after 3 attempts: '+str(exc)) from exc
                        time.sleep(2**attempt)
            ready=True
            with self.lifecycle:conn=self.conn
            if conn is None or self.stopping.is_set():raise ValueError('Worker stopped')
            from file_deadline import exchange
            result=exchange(conn,max(0,deadline-time.monotonic()),row)
            result['_file_processing_seconds']=spent+time.monotonic()-started
            if 'worker_error' in result:raise RuntimeUnavailable('Worker runtime failed: '+result['worker_error'])
            self.consecutive_failures=0
            return result
        except RuntimeUnavailable:
            self.close();raise
        except (OSError,EOFError,ValueError,TimeoutError) as exc:
            self.restarts+=1
            self.close()
            # File timeouts are item failures, not a reason to restart the gallery.
            if isinstance(exc,TimeoutError):self.consecutive_failures=0
            else:self.consecutive_failures+=1
            if ready and self.consecutive_failures>=3:
                raise RuntimeUnavailable('Worker failed on 3 consecutive files: '+str(exc)) from exc
            return {**row,'status':'worker_failed','checks':{check:failure(exc) for check in self.args[1]}}

class ParallelProcessor:
    """Bounded scheduling; each slot owns one persistent local-model process."""
    def __init__(self,config,checks,database,protected,deleted,timeout):
        self.slot_args=(config,checks,database,protected,deleted,timeout)
        self.control=None;self.active_count=0
        if config.get('_live_import_workers'):
            from live_worker_control import WorkerControl
            self.control=WorkerControl(config,database)
        self.slots=[BoundedProcessor(config,checks,database,protected,deleted,timeout)
                    for _ in range(self.control.initial if self.control else config.get('pass_workers',4))]
    def performance(self):
        return self.control.snapshot(self.slots,self.active_count) if self.control else {}
    def close(self):
        for slot in self.slots:slot.close()
    def process_many(self,rows):
        from photo_triage.batch import process_batches
        yield from process_batches(rows,self._process_batch,self.slot_args[0].get('triage_batch_size',500))

    def _process_batch(self,rows):
        pending=iter(rows);cloud_failures=defaultdict(int);cloud_blocked=set()
        if self.control:self.control.last_growth=time.monotonic()
        with ThreadPoolExecutor(max_workers=self.control.maximum if self.control else len(self.slots)) as pool:
            active={}
            exhausted=False
            def schedule(slot,row):
                if row.get('placeholder') and row['library'] in cloud_blocked:
                    return pool.submit(lambda:{**row,'status':'placeholder_unverified',
                        'checks':{check:{'status':'placeholder_unverified','candidate':False,
                            'reason':'Cloud downloads stopped after three consecutive failures'} for check in slot.args[1]}})
                return pool.submit(slot.process_row,row)
            while True:
                target=self.control.poll(len(self.slots)) if self.control else len(self.slots)
                busy={slot for slot,_ in active.values()}
                for slot in list(self.slots):
                    if len(self.slots)>target and slot not in busy:
                        slot.close();self.slots.remove(slot)
                if not exhausted:
                    while len(self.slots)<target:self.slots.append(BoundedProcessor(*self.slot_args))
                    for slot in self.slots:
                        if slot in busy:continue
                        row=next(pending,None)
                        if row is None:exhausted=True;break
                        active[schedule(slot,row)]=(slot,row)
                self.active_count=len(active)
                if self.control:self.control.publish(self.slots,self.active_count)
                if not active:break
                finished,_=wait(active,timeout=2,return_when=FIRST_COMPLETED)
                for future in finished:
                    slot,original=active.pop(future);result=future.result()
                    if original.get('placeholder'):
                        if result['status'] in {'cloud_download_failed','unreadable'}:
                            cloud_failures[original['library']]+=1
                            if cloud_failures[original['library']]==3:
                                cloud_blocked.add(original['library'])
                                print(json.dumps({'stage':'cloud_downloads_held','gallery':original['library'],
                                    'consecutive_failures':3}),flush=True)
                        elif result['status']=='verified_local_bytes' and original['library'] not in cloud_blocked:
                            cloud_failures[original['library']]=0
                    yield result

def ensure_check_results(rows, checks):
    """Report incomplete processor evidence as a gap, never as a passing check."""
    for row in rows:
        results = row.setdefault('checks', {})
        for check in checks:
            if not isinstance(results.get(check), dict) or not results[check].get('status'):
                results[check] = {'status': 'error', 'candidate': False,
                                  'error': 'Processor returned no result for selected check: '+check}


def plans(rows, checks, config, wanted, deleted, review, mappings, issues, partial=False):
    """Global comparisons consume the accumulated rows, never a second traversal."""
    selected=[r for r in rows if r['library'] in {'onedrive','icloud'} and r['kind']!='sidecar']
    result={}
    if set(checks)&{'duplicates','sync'}:
        from two_library_duplicates import build_plan
        data=build_plan(selected,wanted,deleted,review,config.get('icloud_database'),config.get('delivery_ledger'),config.get('pinned_keepers'))
        for kind in ('moves','copies'):
            for entry in data[kind]:
                # Metadata errors and partial inventory cannot authorize an action.
                if partial or issues:entry['hold']=entry['hold'] or 'incomplete_inventory'
                elif entry.get('metadata_hold'):entry['hold']=entry['hold'] or entry['metadata_hold']
                elif entry.get('keeper',{}).get('metadata_hold'):entry['hold']=entry['hold'] or 'keeper_metadata_unavailable'
        for kind,name in (('moves','eligible_extra_files'),('copies','eligible_copies')):
            data['counts'][name]=sum(not r['hold'] for r in data[kind])
        if 'duplicates' in checks:result['duplicates']=data
        if 'sync' in checks:
            import sync_local_photo_folders as sync
            receipts=sync.read_lines(config.get('delivery_ledger'))
            previous=defaultdict(set)
            for receipt in receipts:
                if receipt.get('sha256') and receipt.get('bytes'):
                    previous[receipt.get('target','icloud')].add((receipt['bytes'],receipt['sha256']))
            candidates,_=sync.make_plan(sync.attach_prior_copy_receipts([dict(r) for r in selected],receipts),deleted,wanted,previous,set())
            for entry in candidates:
                entry['hold']=entry['hold'] or ('incomplete_inventory' if partial or issues else entry.get('metadata_hold'))
            candidates=sync.component_holds(candidates,selected)
            result['sync']={'comparison':audit.compare(selected,deleted,review),
                            'copies':candidates,'counts':{'planned':len(candidates),'eligible_copies':sum(not r['hold'] for r in candidates)},
                            'note':'Missing local bytes do not prove a missing cloud asset; complete components stay held'}
    if 'live-photos' in checks:
        import live_photo_cleanup as live
        import sync_local_photo_folders as sync
        metas={r['path'].casefold():r.get('metadata',{}) for r in rows}
        data=live.plan(selected,mappings,sync.read_lines(config.get('delivery_ledger')),metas,wanted|review)
        if partial or issues:
            for entry in data['selected']:entry['hold']='incomplete_inventory'
            data['held']+=data['selected'];data['selected']=[]
            data['counts'].update(eligible_extra_stills=0,eligible_extra_motion_imports=0,held=len(data['held']))
        result['live-photos']=data
    return result

def validate_output_location(output, config):
    output=Path(output).resolve()
    roots=[Path(root).resolve() for root in config['roots'].values()]
    sync_roots=[]
    for name in ('OneDrive','OneDriveConsumer','OneDriveCommercial'):
        if os.environ.get(name):sync_roots.append(Path(os.environ[name]).resolve())
    # Configured gallery ancestors identify the provider root; arbitrary run
    # basenames such as onedrive-meme-rescan are not provider roots.
    for root in roots:
        sync_roots.extend(parent for parent in (root,*root.parents)
                          if parent.name.casefold() in {'onedrive','icloudphotos'}
                          or parent.name.casefold().startswith('onedrive - '))
    if any(output==root or root in output.parents or output in root.parents for root in roots):
        raise ValueError('Output overlaps gallery')
    if any(output==root or root in output.parents for root in sync_roots):
        raise ValueError('Output must be outside sync roots')
    return output


def prepare_reference_text(config):
    # Text preparation also imports ORT. Select the machine runtime first so
    # later checkpoint validation cannot silently fingerprint a CPU fallback.
    from inference_backend import activate, profile
    activate(profile(config))
    scan.prepare_text(config['clip_model'],config.get('dependencies',[]))


def run(config, checks, galleries=None, output=None, limit=0, timeout=300, processor_factory=None, workers=0, action_handler=None):
    from photo_runtime import budget
    from photo_pipeline import PipelineProcessor
    from scan_lifecycle import ScanCancelled,discard_partials,check_stop
    checks=set(checks)
    if not checks or checks-set(CHECKS):raise ValueError('Select known checks')
    if timeout<=0 or limit<0:raise ValueError('Invalid deadline or limit')
    runtime=budget(config,workers);workers=runtime['workers']
    config={**config,'pass_workers':workers,'pass_file_timeout':timeout,'runtime_budget':runtime,
            'persistent_metadata':True,'model_threads':runtime['model_threads']}
    galleries=galleries or ['onedrive','icloud']
    if set(galleries)=={'onedrive','icloud'}:
        config={**config,'analysis_scope':config.get('analysis_scope','shared_content')}
    ordered=processor_factory is None and (set(galleries)=={'onedrive','icloud'} or config.get('single_gallery_cleanup')) and bool(checks & IMAGE_CHECKS)
    if ordered:
        from ordered_photo_pass import OrderedProcessor
        processor_factory=OrderedProcessor
        checks.add('duplicates')
        config={**config,'duplicates_before_filters':True,'filter_source':config.get('analysis_scope','separate_galleries')}
    processor_factory=processor_factory or PipelineProcessor
    if set(checks)&{'duplicates','sync','live-photos'} and not {'onedrive','icloud'}<=set(galleries) and not config.get('single_gallery_cleanup'):
        raise ValueError('Both OneDrive and iCloud are required for library comparisons')
    roots={k:config['roots'][k] for k in dict.fromkeys(galleries)}
    out=Path(output or config['output']/'pass').resolve()
    validate_output_location(out,config)
    wanted,deleted=settings.decisions(config['safe_ledgers'],config['deletion_ledger'])
    review=settings.review_hashes({'reviewing','copy_pending','restore_pending','kept'})
    out.mkdir(parents=True,exist_ok=True);lock=out/'pass.lock'
    with lock.open('x') as stream:stream.write(str(os.getpid()))
    processor=None;started=time.monotonic()
    try:
        from live_photo_cleanup import native_assets
        mappings=native_assets(config['icloud_database']) if 'icloud' in roots and config.get('icloud_database') else {}
        inventory_started=time.perf_counter();rows,issues=inventory(roots,mappings,include_unknown=True) if config.get('include_unknown_files') else inventory(roots,mappings)
        inventory_seconds=time.perf_counter()-inventory_started
        check_stop(out)
        write(out/'inventory-issues.json',issues)
        total=len(rows);partial=bool(limit and total>limit)
        config={**config,'_inventory_complete':not issues and not partial}
        with (out/'inventory.jsonl').open('w',encoding='utf-8') as stream:
            for row in rows:stream.write(json.dumps(row)+'\n')
        # Sidecar records are necessary even if no image checks are selected.
        if limit:rows=rows[:limit]
        if 'junk' in checks and config.get('clip_model'):
            prepare_reference_text(config)
            config['text_embeddings_prepared']=True
        if action_handler:action_handler.start(rows,issues,partial,mappings)
        processor=processor_factory(config,checks,out/'pass-cache.sqlite',wanted if config.get('single_gallery_cleanup') else wanted|review,deleted,timeout)
        if hasattr(processor,'action_handler'):processor.action_handler=action_handler
        if action_handler and hasattr(processor,'prepared_callback'):
            processor.prepared_callback=action_handler.prepared
            processor.preparation_finished=action_handler.preparation_finished
            processor.on_cancel=action_handler.stop
        done=[]
        from scan_health import Coverage
        coverage=Coverage()
        def emit_progress(performance=None):
            if config.get('parent_run'):check_stop(Path(config['parent_run']))
            progress={'processed':len(done),'total':len(rows),'elapsed_seconds':round(time.monotonic()-started,2)}
            progress['coverage']=coverage.snapshot()
            if performance is not None:progress['performance']=performance
            elif hasattr(processor,'performance'):progress['performance']=processor.performance()
            if action_handler:
                summary_path=action_handler.out/'summary.json'
                try:progress['cleanup']=json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {'early_actions':0}
                except (OSError,ValueError):progress['cleanup']={'status':'temporarily_unavailable'}
            write_progress(out/'progress.json',progress)
            if config.get('parent_run'):
                write_progress(Path(config['parent_run'])/'progress.json',{**progress,'active_gallery':galleries[0]})
        if hasattr(processor,'progress_callback'):processor.progress_callback=emit_progress
        # Explicit incomplete marker blocks legacy readers from accepting an old
        # summary or a limited/interrupted scan as a completed library inventory.
        receipt=out/'files.jsonl.complete.json'
        write(receipt,{'complete':False,'reason':'pass_in_progress'})
        with (out/'coverage.jsonl').open('w',encoding='utf-8') as stream:
            results=processor.process_many(rows) if hasattr(processor,'process_many') else (processor.process_row(row) for row in rows)
            for result in results:
                check_stop(out)
                done.append(result)
                coverage.add(result)
                if action_handler:action_handler.accept(result)
                stream.write(json.dumps(result)+'\n')
                if len(done)%100==0:
                    stream.flush();print(json.dumps({'stage':'all_selected_checks','processed':len(done),'total':len(rows)}),flush=True)
                if len(done)%100==0 or len(done)==1:
                    emit_progress()
        streaming=action_handler.close() if action_handler else None
        emit_progress()
        if streaming:config={**config,'pinned_keepers':streaming['keepers']}
        ensure_check_results(done, checks)
        # Export the common inventory for existing executors and per-check review queues.
        media_rows=[row for row in done if row['kind']!='sidecar']
        with (out/'files.jsonl').open('w',encoding='utf-8') as stream:
            for row in media_rows:stream.write(json.dumps(row)+'\n')
        # Candidate-only cross-encoding matches use the decoded-image features;
        # no second gallery walk and no deletion authorization.
        from triage_adapter import write_similarity_report
        write_similarity_report(done,out/'visual-candidates.json')
        generated=plans([r for r in done if not r.get('early_move')],checks,config,wanted,deleted,review,mappings,issues,partial)
        for name,data in generated.items():write(out/(name+'-plan.json'),data)
        counts={};candidate_counts={}
        for check in sorted(checks):
            folder=out/check;folder.mkdir(exist_ok=True)
            counts[check]=dict(Counter(r['checks'][check]['status'] for r in done))
            candidate_counts[check]=sum(bool(r['checks'][check].get('candidate')) for r in done)
            with (folder/'coverage.jsonl').open('w',encoding='utf-8') as stream:
                for row in done:
                    payload={**row,**row['checks'][check]};payload.pop('checks',None)
                    stream.write(json.dumps(payload)+'\n')
        gaps=bool(issues) or any(r['status'] not in {'verified_local_bytes','excluded_deleted_asset','excluded_cached_rendition'} for r in done)
        check_gaps=any(any(s in {'error','needs_review','analyzed_with_check_gaps','analyzed_with_sample_gaps','cataloged_with_holds'} for s in counter) for counter in counts.values()) or any(r.get('metadata',{}).get('status')=='error' for r in done)
        if 'nsfw' in checks:
            from sexual_content import write_uncertain
            write_uncertain(out,done)
        summary={'status':'sample_complete' if partial else 'complete_with_gaps' if gaps or check_gaps else 'complete',
            'selected_checks':sorted(checks),'galleries':list(roots),'inventoried':total,'processed':len(done),'files':len(media_rows),
            'workers':workers,
            'check_counts':counts,'candidate_counts':candidate_counts,
            'plan_counts':{name:data.get('counts',{}) for name,data in generated.items()},
            'cache_hits':sum(bool(c.get('cache_reused')) for r in done for c in r['checks'].values()),
            'inventory_issues':issues,'elapsed_seconds':round(time.monotonic()-started,2),
            'source_mutations':streaming['early_actions'] if streaming else 0,'cloud_equality_verified':False,
            'streaming_cleanup':streaming,
            'ordered_processing':getattr(processor,'ordered_summary',{}),
            'performance':{**(processor.performance() if hasattr(processor,'performance') else {}),'inventory_seconds':inventory_seconds},
            'limitations':['Cloud originals use bounded downloads; unavailable bytes remain pending' if config.get('download_originals') else 'Resident files only; placeholders are not downloaded','Scores nominate review candidates; no deletion or keep decision',
                          'Still-image and sampled-video quality/junk/NSFW/TikTok; first animation frame; no audio or intervening video frames',
                          'Native Live Photo motion association is metadata evidence, not fresh cloud-byte verification']}
        write(out/'summary.json',summary)
        (out/'report.md').write_text('# Combined photo pass\n\n'+summary['status']+'; '+str(len(done))+' files checked. Verified early moves: '+str(summary['source_mutations'])+'.\n\n'+
            '\n'.join('- '+name+': '+(str(candidate_counts[name])+' review candidates; ' if name in IMAGE_CHECKS else '')+json.dumps(counts[name]) for name in sorted(checks))+
            '\n\n'+ '\n'.join('- ['+name+' plan]('+name+'-plan.json): '+json.dumps(data.get('counts',{})) for name,data in generated.items())+
            '\n\nRead the per-check coverage files and action plans. Errors and unavailable files remain gaps. Uncertain sexual-content findings stay in place; see nsfw-needs-review.csv when NSFW was requested.\n',encoding='utf-8')
        if not partial and not issues:
            from two_library_duplicates import scan_receipt
            scan_receipt(out/'files.jsonl',media_rows)
        summary['coverage']=coverage.snapshot()
        write(out/'summary.json',summary)
        print(json.dumps(summary),flush=True)
        return summary
    except RuntimeUnavailable as exc:
        if action_handler:getattr(action_handler,'stop',action_handler.close)()
        if processor:processor.close()
        diagnostic={'status':'blocked_runtime','error':str(exc),'retryable':True,
                    'completed_moves_preserved':True,'saved_inventory_reuse_authorized':False}
        write(out/'failure.json',diagnostic)
        write_progress(out/'progress.json',{**diagnostic,'processed':len(done),'total':len(rows),
                       'coverage':coverage.snapshot()})
        raise
    except (ScanCancelled,KeyboardInterrupt):
        if action_handler:getattr(action_handler,'stop',action_handler.close)()
        if processor:processor.close()
        if config.get('import_mode'):
            write(out/'stopped.json',{'status':'stopped','completed_actions_preserved':True})
        else:discard_partials(out)
        raise
    finally:
        if processor:processor.close()
        if action_handler:action_handler.close()
        lock.unlink(missing_ok=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checks',nargs='+',required=True,choices=CHECKS)
    parser.add_argument('--gallery',action='append',choices=['onedrive','icloud','amazon'])
    parser.add_argument('--output',type=Path);parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--file-timeout',type=float,default=300)
    parser.add_argument('--workers',type=int,default=0,help='Inference workers; 0 selects a CPU/RAM-aware budget')
    parser.add_argument('--model-threads',type=int)
    parser.add_argument('--io-workers',type=int)
    parser.add_argument('--download-workers',type=int)
    args=parser.parse_args();config=settings.load()
    config={**config,**{name:getattr(args,name) for name in ('model_threads','io_workers','download_workers') if getattr(args,name) is not None}}
    summary=run(config,args.checks,args.gallery,args.output,args.limit,args.file_timeout,workers=args.workers)
    return 0 if summary['status']=='complete' else 2

if __name__=='__main__':raise SystemExit(main())
