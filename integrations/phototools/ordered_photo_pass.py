"""Remove exact extras before screening unique content within each gallery."""
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import closing
from pathlib import Path
import time
import uuid

from photo_pipeline import PipelineProcessor, alias_result
from photo_pass_analysis import failure
from scan_lifecycle import check_stop, ScanCancelled


class OrderedProcessor(PipelineProcessor):
    def __init__(self,config,checks,database,protected,deleted,timeout):
        super().__init__(config,checks,database,protected,deleted,timeout)
        self.protected=protected;self.deleted=deleted;self.action_handler=None
        self.phase='hash_gallery_files';self.ordered_summary={
            'unique_filter_runs':0,'apple_only_staged':0,'staging_holds':0}

    def remove_duplicates(self,rows):
        if not self.config.get('duplicate_cleanup_enabled',True):
            if self.action_handler:self.action_handler.duplicates_before_filters=True
            self.ordered_summary.update(duplicates_before_filters=False,duplicate_cleanup_disabled=True,duplicate_actions={})
            return
        import photo_pass
        import photo_settings as settings
        import two_library_duplicates as duplicates
        from review_queue import FingerprintWorker,locked
        handler=self.action_handler
        if handler:handler.duplicates_before_filters=True
        mappings=handler.mappings if handler else {}
        wanted,deleted=settings.decisions(self.config['safe_ledgers'],self.config['deletion_ledger'])
        review=settings.review_hashes({'reviewing','copy_pending','restore_pending','kept'})
        plan=photo_pass.plans(rows,{'duplicates'},self.config,wanted,deleted,review,mappings,
            [] if self.config.get('_inventory_complete') else ['incomplete_inventory'])['duplicates']
        target=self.run/'streaming'/'duplicates';target.mkdir(parents=True,exist_ok=True)
        duplicates.write_json(target/'plan.json',plan)
        counts={};verification=None
        if handler and handler.enabled and plan['counts']['eligible_extra_files']:
            with closing(FingerprintWorker(timeout=45,allow_cloud=False)) as reader:
                with locked(target/'state.json'):
                    counts=duplicates.apply_moves(plan,self.config,target,
                        {k:self.config['roots'][k] for k in ('onedrive','icloud')},reader,
                        check=lambda:check_stop(self.run))
                    verification=duplicates.verify_batch(self.config,target,self.config['roots'],reader)
                # Finish all identity callbacks before recording duplicate removals.
                handler.pool.submit(lambda:None).result()
                handler._record_removed(rows,'duplicate')
                handler.stats['duplicate_moves']+=sum(counts.values())
        self.ordered_summary={'duplicates_before_filters':True,'filter_source':self.config.get('analysis_scope','separate_galleries'),
            'duplicate_actions':counts,'duplicate_verification':verification,
            'duplicate_plan_counts':plan['counts'],'apple_only_staged':0,'staging_holds':0,
            'unique_filter_runs':0}

    def classify_owner(self,slot,original):
        """Apple-only bytes use temporary, nonsynced working input, never a public import."""
        import photo_pass
        from file_deadline import copy_bytes
        started=time.monotonic();temporary=None
        try:
            owner=dict(original)
            if original.get('metadata') is not None:owner['_prepared_metadata']=original['metadata']
            if self.config.get('analysis_scope')=='onedrive' and owner['library']=='icloud' and owner['kind']!='sidecar':
                check_stop(self.run);photo_pass.stable(original)
                stage=self.run/'filter-staging'
                stage.mkdir(exist_ok=True)
                if stage.is_symlink() or stage.is_junction():raise ValueError('Linked filter staging')
                for root in self.config['roots'].values():
                    if stage.resolve()==root.resolve() or root.resolve() in stage.resolve().parents:
                        raise ValueError('Filter staging overlaps gallery')
                import merge_photos as media
                media.space(stage,owner['bytes'],50*1024**3)
                temporary=stage/(uuid.uuid4().hex+Path(owner['path']).suffix)
                remaining=self.timeout-float(owner.get('_file_processing_seconds',0))
                if remaining<=0:raise TimeoutError('File processing deadline exhausted before staging')
                actual=copy_bytes(Path(owner['path']),temporary,timeout=remaining)
                photo_pass.stable(original)
                if (actual['sha256'],actual['bytes'])!=(owner['sha256'],owner['bytes']):
                    raise ValueError('Apple-only working copy differs from freshly hashed original')
                stat=temporary.stat()
                owner.update(path=str(temporary),root=str(stage),library='onedrive',label='onedrive',
                    placeholder=False,mtime_ns=stat.st_mtime_ns,
                    _file_processing_seconds=float(owner.get('_file_processing_seconds',0))+time.monotonic()-started)
            check_stop(self.run)
            result=slot.process_row(owner)
            return result,owner['path'],bool(temporary)
        except ScanCancelled:raise
        except (OSError,ValueError,TimeoutError) as exc:
            return {**original,'checks':{c:failure(exc) for c in self.checks}},None,bool(temporary)
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)

    def classify_group(self,slot,group):
        from duplicate_scan_handoff import reconcile
        try:
            group[:]=reconcile(group,self.run)
            owner=min((r for r in group if not r.get('early_move')),
                      key=lambda r:(r['library']!='onedrive',r['path'].casefold()))
            result,source,staged=self.classify_owner(slot,owner)
            # A concurrently approved duplicate action may finish after scheduling.
            if any('FileNotFoundError' in c.get('error','') for c in result.get('checks',{}).values()):
                group[:]=reconcile(group,self.run)
                replacement=min((r for r in group if not r.get('early_move')),
                                key=lambda r:(r['library']!='onedrive',r['path'].casefold()))
                if replacement['path']!=owner['path']:
                    return self.classify_owner(slot,replacement)
            return result,source,staged
        except (OSError,ValueError) as exc:
            return {**group[0],'checks':{c:failure(exc) for c in self.checks}},None,False

    def process_many(self,rows):
        import photo_pass
        self.phase='hash_gallery_files'
        prepared=list(super().process_many(rows,preparation_only=True))
        for row in prepared:
            meta=row.pop('_prepared_metadata',None)
            if meta is not None:
                row['metadata']=meta
                row['content_identifier']=meta.get('content_identifier')
                row['metadata_hold']=None if meta.get('status')=='cataloged' else 'association_metadata_unavailable'
            row['checks']={c:{'status':'included_in_inventory_plan','candidate':False} for c in self.checks} if row['status']=='verified_local_bytes' else row['checks']
        check_stop(self.run);self.phase='remove_within_gallery_duplicates'
        self.remove_duplicates(prepared)
        check_stop(self.run);self.phase='filter_unique_gallery_content'
        groups=defaultdict(list)
        for row in prepared:
            if row.get('early_move'):
                row['checks']={c:{'status':'removed_exact_duplicate_before_filters','candidate':False} for c in self.checks}
                yield row
            elif row['status']!='verified_local_bytes':yield row
            else:
                key=(row['sha256'],row['bytes'],row['kind'])
                if self.config.get('analysis_scope') not in {'onedrive','shared_content'}:key=(row['library'],*key)
                groups[key].append(row)
        # Separate queues avoid repeatedly searching every pending photo for a
        # video (quadratic for photo-only galleries). Keep each queue ordered.
        photos=deque();videos=deque();active={}
        from photo_triage.batch import batches
        pending_batches=iter(batches(sorted(groups.values(),key=lambda group:(group[0]['library']!='onedrive',group[0]['path'].casefold())),self.config.get('triage_batch_size',500)))
        batches_finished=False
        self.inference_slots=[photo_pass.BoundedProcessor({**self.config,'pipeline_stage':'inference'},
            self.checks,self.database,self.protected,self.deleted,self.timeout) for _ in range(self.runtime['workers'])]
        free=list(self.inference_slots);video_running=0
        self.pool=ThreadPoolExecutor(max_workers=len(free))
        try:
            while photos or videos or active or not batches_finished:
                if not photos and not videos and not active:
                    next_batch=next(pending_batches,None)
                    if next_batch is None:
                        batches_finished=True
                        break
                    for group in next_batch:
                        (videos if group[0]['kind']=='video' else photos).append(group)
                    self.ordered_summary['triage_batches_started']=self.ordered_summary.get('triage_batches_started',0)+1
                check_stop(self.run)
                if self.progress_callback and time.monotonic()-self.last_progress>=2:
                    self.progress_callback(self.performance());self.last_progress=time.monotonic()
                while (photos or videos) and free:
                    can_video=bool(videos) and video_running<self.runtime['video_workers']
                    if can_video and (active or not photos):group=videos.popleft()
                    elif photos:group=photos.popleft()
                    else:break
                    owner=min(group,key=lambda r:(r['library']!='onedrive',r['path'].casefold()))
                    slot=free.pop()
                    if owner['kind']=='video':video_running+=1
                    active[self.pool.submit(self.classify_group,slot,group)]=(slot,owner,group)
                self.stats.update({'ready':len(photos)+len(videos),'active':len(active)})
                if not active:continue
                finished,_=wait(active,timeout=.2,return_when=FIRST_COMPLETED)
                for future in finished:
                    slot,owner,group=active.pop(future);result,source,staged=future.result();free.append(slot)
                    for name,value in result.get('checks',{}).items():
                        message=value.get('error','')
                        if any(marker in message for marker in ('DLL load failed','cannot import name','No module named',
                            'Importing the numpy','Application Control policy','Bundled OCR models missing','[WinError 206]')):
                            self.infrastructure_failures[name]+=1
                            if self.infrastructure_failures[name]>=3:
                                raise photo_pass.RuntimeUnavailable(name+' dependency failed repeatedly: '+message)
                        elif value.get('status')!='error':self.infrastructure_failures[name]=0
                    if owner['kind']=='video':video_running-=1
                    self.ordered_summary['unique_filter_runs']+=1
                    self.ordered_summary['apple_only_staged']+=int(staged and source is not None)
                    self.ordered_summary['staging_holds']+=int(source is None)
                    # Map the same failure as well as success: never re-run models on aliases.
                    from duplicate_scan_handoff import resolved
                    mapped=[resolved(r,self.checks) if r.get('duplicate_keeper') else
                            alias_result(result,r,self.checks,preserve_metadata=True) for r in group]
                    for value in mapped:
                        value['filter_source']=source
                        value['inference_shared']=value['path']!=(owner['path'] if staged else source)
                        if not value['inference_shared']:
                            value['timings']={**value.get('timings',{}),**result.get('timings',{})}
                        self.stage_seconds.update(value.get('timings',{}))
                    for value in mapped:yield value
            self.phase='filters_complete'
        except (ScanCancelled,KeyboardInterrupt):
            if self.on_cancel:self.on_cancel()
            raise
        finally:
            self.close()
            stage=self.run/'filter-staging'
            if stage.exists() and not any(stage.iterdir()):stage.rmdir()

    def performance(self):
        return {**super().performance(),'phase':self.phase,'ordered_processing':self.ordered_summary}
