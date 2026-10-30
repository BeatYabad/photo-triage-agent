import hashlib
from pathlib import Path
import tempfile
import unittest
import threading
from unittest.mock import patch

import photo_pass
from ordered_photo_pass import OrderedProcessor
from test_photo_pipeline import row


class OrderedTests(unittest.TestCase):
    def test_mixed_queue_preserves_video_concurrency_limit_and_all_results(self):
        base=self.Slot;running=0;maximum=0;photos_finished=0;lock=threading.Lock()
        release=threading.Event();test=self
        class Slot(base):
            def process_row(self,value):
                nonlocal running,maximum,photos_finished
                if self.stage!='inference':return super().process_row(value)
                if value['kind']!='video':
                    result=super().process_row(value)
                    with lock:
                        photos_finished+=1
                        if photos_finished==6:release.set()
                    return result
                with lock:running+=1;maximum=max(maximum,running)
                try:
                    test.assertTrue(release.wait(5),'Video work blocked all photo inference slots')
                    return super().process_row(value)
                finally:
                    with lock:running-=1
        rows=[self.file(f'{i}.jpg',content=str(i).encode()) for i in range(12)]
        for row in rows[::2]:row['kind']='video'
        with patch.object(photo_pass,'BoundedProcessor',Slot),patch.object(self.processor,'remove_duplicates'):
            result=list(self.processor.process_many(rows))
        self.assertEqual(maximum,1)
        self.assertEqual({r['path'] for r in result},{r['path'] for r in rows})

    def test_group_batches_keep_duplicate_barrier_and_all_results(self):
        self.config['triage_batch_size']=2
        rows=[self.file(f'batch-{i}.jpg',content=str(i).encode()) for i in range(5)]
        def barrier(prepared):
            self.assertEqual(len(prepared),5)
            self.assertTrue(all(stage=='prepare' for stage,_ in self.events))
        with patch.object(self.processor,'remove_duplicates',side_effect=barrier):
            results=list(self.processor.process_many(rows))
        self.assertEqual(len(results),5)
        self.assertEqual(self.processor.ordered_summary['triage_batches_started'],3)

    def test_photo_only_queue_work_grows_linearly(self):
        class Kind(str):
            comparisons=0
            __hash__=str.__hash__
            def __eq__(self,other):
                Kind.comparisons+=1
                return str.__eq__(self,other)
            def __ne__(self,other):
                Kind.comparisons+=1
                return str.__ne__(self,other)
        rows=[self.file(f'photo-{i}.jpg',content=str(i).encode()) for i in range(100)]
        for row in rows:row['kind']=Kind('photo')
        with patch.object(self.processor,'remove_duplicates'):
            result=list(self.processor.process_many(rows))
        self.assertEqual(len(result),100)
        self.assertLess(Kind.comparisons,2000)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.events=[]
        self.config={'roots':{label:self.root/label for label in ('onedrive','icloud')},
            'analysis_scope':'onedrive',
            'runtime_budget':{'workers':2,'io_workers':2,'download_workers':1,'video_workers':1,'queue_capacity':4},
            'safe_ledgers':[],'deletion_ledger':self.root/'deleted.json','_inventory_complete':True}
        for root in self.config['roots'].values():root.mkdir()
        events=self.events
        class Slot:
            def __init__(self,config,*args):self.stage=config['pipeline_stage']
            def close(self):pass
            def process_row(self,value):
                events.append((self.stage,value['path']))
                if self.stage=='prepare':return {**value,'status':'verified_local_bytes',
                    'sha256':hashlib.sha256(Path(value['path']).read_bytes()).hexdigest(),
                    '_prepared_metadata':{'status':'cataloged','content_identifier':value['library'],'live_photo_video_index':8192}}
                return {**value,'checks':{'junk':{'status':'analyzed','candidate':False},
                    'duplicates':{'status':'included_in_inventory_plan','candidate':False}}}
        self.Slot=Slot
        for target in ('photo_settings.decisions','photo_settings.review_hashes'):
            guard=patch(target,return_value=(set(),set()) if target.endswith('decisions') else set())
            guard.start();self.addCleanup(guard.stop)
        guard=patch.object(photo_pass,'BoundedProcessor',Slot);guard.start();self.addCleanup(guard.stop)
        guard=patch('merge_photos.space');guard.start();self.addCleanup(guard.stop)
        self.processor=OrderedProcessor(self.config,{'junk','duplicates'},self.root/'pass.sqlite',set(),set(),30)
        self.addCleanup(self.processor.close)

    def file(self,name,library='onedrive',content=b'fixture'):
        path=self.config['roots'][library]/name;path.write_bytes(content)
        value=row(path,library);value['root']=str(self.config['roots'][library]);return value

    def test_barrier_and_onedrive_preference_preserve_native_metadata(self):
        apple=self.file('apple.jpg','icloud');one=self.file('one.jpg')
        def dedup(rows):
            self.assertEqual(len(self.events),2)
            self.assertTrue(all(stage=='prepare' for stage,_ in self.events))
            self.assertEqual({r['content_identifier'] for r in rows},{'onedrive','icloud'})
            self.events.append(('duplicates_complete',''))
        with patch.object(self.processor,'remove_duplicates',side_effect=dedup):
            results=list(self.processor.process_many([apple,one]))
        inference=[p for s,p in self.events if s=='inference']
        self.assertEqual(inference,[one['path']])
        self.assertEqual({r['content_identifier'] for r in results},{'onedrive','icloud'})
        self.assertEqual({r['path'] for r in results},{apple['path'],one['path']})
        self.assertTrue(all(not r['native_live_photo'] for r in results))

    def test_failed_filter_shared_once_without_alias_retry(self):
        base=self.Slot;events=self.events
        class Failed(base):
            def process_row(self,value):
                result=super().process_row(value)
                if self.stage=='inference':result['checks']['junk']={'status':'error','candidate':False}
                return result
        with patch.object(photo_pass,'BoundedProcessor',Failed),patch.object(self.processor,'remove_duplicates'):
            results=list(self.processor.process_many([self.file('a.jpg'),self.file('b.jpg','icloud')]))
        self.assertEqual(sum(s=='inference' for s,_ in events),1)
        self.assertTrue(all(r['checks']['junk']['status']=='error' for r in results))

    def test_apple_only_staged_off_gallery_without_public_import(self):
        apple=self.file('only.mp4','icloud');apple['kind']='video'
        with patch.object(self.processor,'remove_duplicates'):
            results=list(self.processor.process_many([apple]))
        inferred=next(Path(p) for s,p in self.events if s=='inference')
        self.assertEqual(inferred.parent,self.root/'filter-staging')
        self.assertFalse(inferred.exists());self.assertTrue(Path(apple['path']).exists())
        self.assertEqual(results[0]['path'],apple['path'])
        self.assertFalse(any(self.config['roots']['onedrive'].iterdir()))

    def test_staging_failure_holds_without_classifying_apple_original(self):
        with patch.object(self.processor,'remove_duplicates'),patch('file_deadline.copy_bytes',side_effect=TimeoutError('stalled')):
            results=list(self.processor.process_many([self.file('only.jpg','icloud')]))
        self.assertFalse(any(s=='inference' for s,_ in self.events))
        self.assertEqual(results[0]['checks']['junk']['status'],'error')

    def test_removed_duplicate_never_reaches_filters(self):
        one=self.file('one.jpg');extra=self.file('extra.jpg');apple=self.file('apple.jpg','icloud')
        def dedup(rows):
            for value in rows:
                if value['path']==extra['path']:
                    Path(value['path']).unlink();value['early_move']='duplicate'
        with patch.object(self.processor,'remove_duplicates',side_effect=dedup):
            results=list(self.processor.process_many([extra,apple,one]))
        self.assertEqual(sum(s=='inference' for s,_ in self.events),1)
        self.assertEqual(next(r for r in results if r['path']==extra['path'])['checks']['junk']['status'],
                         'removed_exact_duplicate_before_filters')

    def test_separate_galleries_use_their_own_sources_and_results(self):
        self.processor.config['analysis_scope']='separate_galleries'
        one=self.file('one.jpg');apple=self.file('apple.jpg','icloud')
        with patch.object(self.processor,'remove_duplicates'):
            results=list(self.processor.process_many([one,apple]))
        self.assertEqual({p for stage,p in self.events if stage=='inference'},{one['path'],apple['path']})
        self.assertFalse(any(r['inference_shared'] for r in results))
        self.assertFalse((self.root/'filter-staging').exists())

    def test_shared_content_checks_once_without_staging_apple_only_media(self):
        self.processor.config['analysis_scope']='shared_content'
        one=self.file('one.jpg');apple=self.file('renamed.jpg','icloud')
        unique=self.file('unique.jpg','icloud',b'apple only')
        with patch.object(self.processor,'remove_duplicates'):
            results=list(self.processor.process_many([apple,unique,one]))
        self.assertEqual({p for s,p in self.events if s=='inference'},{one['path'],unique['path']})
        self.assertEqual(len(results),3)
        self.assertTrue(next(r for r in results if r['path']==apple['path'])['inference_shared'])
        self.assertFalse((self.root/'filter-staging').exists())


if __name__=='__main__':unittest.main()
