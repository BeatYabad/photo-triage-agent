import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import photo_pass as runner
from photo_pass_analysis import Analyzer

class FakeAnalyzer:
    def __init__(self):self.calls=[];self.changed=None
    def rule(self,check):return 'fixture-'+check
    def metadata(self,path):
        self.calls.append(('metadata',str(path)))
        return {'status':'cataloged','embedded':{'XMP:Subject':[path.read_text()]},'content_identifier':None}
    def image(self,path,checks):
        self.calls.append(('image',str(path),set(checks)))
        if self.changed:path.write_bytes(b'changed during analysis')
        return {name:{'status':'analyzed','candidate':name=='quality','holds':['visual_review_required']} for name in checks}
    video=image

def row(path,library='onedrive',kind='photo'):
    st=path.stat()
    return {'library':library,'label':library,'root':str(path.parent),'path':str(path),'kind':kind,
            'bytes':st.st_size,'mtime_ns':st.st_mtime_ns,'status':'inventoried','placeholder':False,'native_live_photo':False}

class PassTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.model=FakeAnalyzer()
    def processor(self,checks,protected=None,deleted=None):
        result=runner.FileProcessor({'require_media_metadata':True},checks,self.root/'cache.sqlite',protected or set(),deleted or set(),self.model)
        self.addCleanup(result.close);return result
    def photo(self,name='a.jpg',content=b'fixture'):
        p=self.root/name;p.write_bytes(content);return p
    def test_one_hash_and_combined_dispatch_per_file(self):
        p=self.photo();processor=self.processor(['quality','junk','nsfw','tiktok','duplicates'])
        with patch.object(runner,'fingerprint',wraps=runner.fingerprint) as hashed:
            result=processor.process(row(p))
        self.assertEqual(hashed.call_count,1)
        self.assertEqual(len(self.model.calls),2)
        self.assertEqual(self.model.calls[1][2],{'quality','junk','nsfw','tiktok'})
        self.assertEqual(set(result['checks']),{'quality','junk','nsfw','tiktok','duplicates'})
    def test_exact_alias_reuses_all_checks_but_is_hashed(self):
        p=self.photo();q=self.photo('b.jpg');processor=self.processor(['quality','junk'])
        processor.process(row(p))
        with patch.object(runner,'fingerprint',wraps=runner.fingerprint) as hashed:result=processor.process(row(q))
        self.assertEqual(hashed.call_count,1)
        self.assertEqual(len(self.model.calls),2)
        self.assertTrue(all(v['cache_reused'] for v in result['checks'].values()))
    def test_restart_reuses_successful_content_cache(self):
        p=self.photo();processor=self.processor(['quality']);processor.process(row(p))
        other=runner.FileProcessor({'require_media_metadata':True},['quality'],self.root/'cache.sqlite',set(),set(),self.model);self.addCleanup(other.close)
        self.assertTrue(other.process(row(p))['checks']['quality']['cache_reused'])
        self.assertEqual(len(self.model.calls),2)
    def test_adding_a_check_only_runs_new_check(self):
        p=self.photo();self.processor(['quality']).process(row(p))
        result=self.processor(['quality','junk']).process(row(p))
        self.assertTrue(result['checks']['quality']['cache_reused'])
        self.assertEqual(self.model.calls[-1][2],{'junk'})
    def test_changed_bytes_invalidate_results(self):
        p=self.photo();processor=self.processor(['quality']);a=processor.process(row(p))
        p.write_bytes(b'new bytes');b=processor.process(row(p))
        self.assertNotEqual(a['sha256'],b['sha256']);self.assertFalse(b['checks']['quality']['cache_reused'])
    def test_rule_change_invalidates_only_affected_check(self):
        p=self.photo();self.processor(['quality','junk']).process(row(p))
        with patch.object(self.model,'rule',side_effect=lambda c:'new' if c=='junk' else 'fixture-'+c):
            result=self.processor(['quality','junk']).process(row(p))
        self.assertTrue(result['checks']['quality']['cache_reused']);self.assertEqual(self.model.calls[-1][2],{'junk'})
    def test_protected_and_deleted_never_screened(self):
        p=self.photo();digest=hashlib.sha256(p.read_bytes()).hexdigest()
        for protected,deleted,status in (({digest},set(),'held_user_exclusion'),
                (set(),{digest},'held_intentional_deletion'),({digest},{digest},'held_user_exclusion')):
            with self.subTest(status=status,protected=bool(protected),deleted=bool(deleted)):
                result=self.processor(['quality','junk'],protected,deleted).process(row(p))
                self.assertFalse(self.model.calls);self.assertEqual(result['status'],'verified_local_bytes')
                self.assertEqual(result['checks']['junk']['status'],status)
                self.assertTrue(result['metadata_hold'])
    def test_placeholder_never_opened(self):
        p=self.photo();value={**row(p),'placeholder':True}
        with patch.object(runner,'fingerprint',side_effect=AssertionError('must not read')) as hashed, \
             patch.object(runner,'download_original',side_effect=AssertionError('must not hydrate')) as download:
            result=self.processor(['quality','nsfw','junk','duplicates']).process(value)
        hashed.assert_not_called();download.assert_not_called()
        self.assertEqual(result['status'],'placeholder_unverified');self.assertFalse(self.model.calls)
    def test_downloaded_original_is_freshly_hashed_before_inference(self):
        p=self.photo();value={**row(p),'placeholder':True}
        processor=self.processor(['nsfw']);processor.config['download_originals']=True
        with patch.object(runner,'download_original',return_value={**value,'placeholder':False,'cloud_download_verified':False}),\
             patch.object(runner,'fingerprint',wraps=runner.fingerprint) as hashed:
            result=processor.process(value)
        self.assertEqual(hashed.call_count,1);self.assertTrue(result['cloud_download_verified'])
        self.assertEqual(result['sha256'],hashlib.sha256(b'fixture').hexdigest())
    def test_download_failure_never_becomes_safe_or_runs_inference(self):
        p=self.photo();processor=self.processor(['nsfw']);processor.config['download_originals']=True
        with patch.object(runner,'download_original',side_effect=OSError('provider unavailable')):
            result=processor.process({**row(p),'placeholder':True})
        self.assertEqual(result['status'],'cloud_download_failed');self.assertFalse(self.model.calls)
        self.assertNotIn('sha256',result)
    def test_cloud_download_circuit_does_not_stop_resident_processing(self):
        attempted=[]
        class Slot:
            args=({},['nsfw'])
            def process_row(self,value):
                attempted.append(value['path'])
                return {**value,'status':'cloud_download_failed' if value['placeholder'] else 'verified_local_bytes'}
        processor=runner.ParallelProcessor({'pass_workers':1},['nsfw'],self.root/'unused.sqlite',set(),set(),1)
        processor.slots=[Slot()]
        values=[{**row(self.photo(str(i)+'.jpg')),'placeholder':True} for i in range(8)]
        values.append(row(self.photo('resident.jpg')))
        results=list(processor.process_many(values))
        self.assertEqual(len(attempted),4);self.assertEqual(len(results),9)
        self.assertEqual(results[-1]['status'],'verified_local_bytes')
        self.assertEqual(sum(r['status']=='placeholder_unverified' for r in results),5)
    def test_changed_source_discarded_and_not_cached(self):
        p=self.photo();self.model.changed=True;processor=self.processor(['quality'])
        result=processor.process(row(p));self.assertEqual(result['status'],'unreadable')
        self.assertNotIn('sha256',result)
        self.assertEqual(processor.db.execute('SELECT COUNT(*) FROM results').fetchone()[0],0)
    def test_metadata_error_does_not_block_image_checks(self):
        p=self.photo()
        with patch.object(self.model,'metadata',side_effect=ValueError('metadata failure')):
            result=self.processor(['quality','sync']).process(row(p))
        self.assertEqual(result['checks']['quality']['status'],'analyzed')
        self.assertEqual(result['metadata']['status'],'error');self.assertTrue(result['metadata_hold'])
    def test_shared_decode_and_ocr_even_if_quality_fails(self):
        from PIL import Image
        p=self.root/'real.png';Image.new('RGB',(96,96),(45,95,150)).save(p)
        analyzer=Analyzer({});opened=Image.open
        with patch('PIL.Image.open',wraps=opened) as decoding, \
             patch.object(analyzer,'ocr',return_value=([],(96,96))) as ocr, \
             patch.object(analyzer,'model',side_effect=lambda c:(None,None) if c=='clip' else None), \
             patch('quality_analysis.analyze_image',side_effect=ValueError('bad metric')), \
             patch('nsfw_analysis.analyze_image',return_value={'status':'analyzed','candidate':False}), \
             patch('scan_reference_photos.analyze_image',return_value={'status':'analyzed','candidate':False,'holds':[]}):
            results=analyzer.image(p,{'quality','junk','nsfw','tiktok'})
        self.assertEqual(decoding.call_count,1);self.assertEqual(ocr.call_count,1)
        self.assertEqual(results['quality']['status'],'error')
        self.assertTrue(all(results[c]['status']=='analyzed' for c in ('junk','nsfw','tiktok')))
    def test_video_capture_and_overlapping_frames_shared(self):
        import cv2
        import numpy as np
        import nsfw_analysis as nsfw
        import tiktok_video_triage as tiktok
        from unittest.mock import Mock
        capture=Mock();capture.isOpened.return_value=True
        values={cv2.CAP_PROP_FPS:2,cv2.CAP_PROP_FRAME_COUNT:6,cv2.CAP_PROP_FRAME_WIDTH:32,cv2.CAP_PROP_FRAME_HEIGHT:48}
        capture.get.side_effect=values.get;capture.read.return_value=(True,np.zeros((48,32,3),np.uint8))
        capture.grab.return_value=True
        capture.retrieve.return_value=capture.read.return_value
        analyzer=Analyzer({});detector=Mock();detector.detect.return_value=[]
        meta={'platform_metadata_markers':[],'camera_metadata':False}
        with patch('cv2.VideoCapture',return_value=capture) as opening, patch('video_decode.endpoint_readable',return_value=True), \
             patch.object(analyzer,'model',return_value=detector), \
             patch.object(analyzer,'ocr',return_value=([],(32,48))) as ocr, \
             patch.object(tiktok,'metadata',return_value=meta):
            results=analyzer.video(self.root/'fake.mov',{'nsfw','tiktok','quality'})
        times=set(nsfw.samples(3,2))|{max(0,min(2.5,3*f)) for f in tiktok.DEEP_SAMPLES}
        self.assertEqual(opening.call_count,1);self.assertEqual(capture.grab.call_count,6)
        self.assertEqual(ocr.call_count,len({max(0,min(2.5,3*f)) for f in tiktok.DEEP_SAMPLES}))
        self.assertEqual(results['nsfw']['status'],'analyzed');self.assertEqual(results['quality']['status'],'analyzed')
        self.assertTrue(results['quality']['candidate'])
        capture.release.assert_called_once()
    def test_unassociated_index_cannot_set_native_live_photo_status(self):
        p=self.photo();processor=self.processor(['duplicates'])
        meta={'status':'cataloged','content_identifier':None,'live_photo_video_index':8192}
        with patch.object(self.model,'metadata',return_value=meta):result=processor.process(row(p))
        self.assertFalse(result['native_live_photo'])
        self.assertEqual(result['metadata']['live_photo_video_index'],8192)

    def test_video_timestamp_failure_uses_verified_endpoint_and_rejects_decode_errors(self):
        import cv2
        import numpy as np
        from unittest.mock import Mock
        for recover in (True,False):
            with self.subTest(recover=recover):
                capture=Mock();capture.isOpened.return_value=True
                capture.get.side_effect={cv2.CAP_PROP_FPS:2,cv2.CAP_PROP_FRAME_COUNT:6}.get
                frame=np.zeros((32,32,3),np.uint8)
                capture.read.side_effect=[(True,frame),(False,None)]
                fallback=Mock();fallback.read.side_effect=None if recover else ValueError('corrupt stream')
                fallback.read.return_value=frame
                fallback.fps=2;fallback.count=6;fallback.duration=3
                fallback.iter_samples.side_effect=(lambda times:iter((s,frame) for s in times)) if recover else ValueError('corrupt stream')
                analyzer=Analyzer({})
                with patch('cv2.VideoCapture',return_value=capture),patch('video_decode.endpoint_readable',return_value=False),patch('nsfw_analysis.samples',return_value=[0,2.5]), \
                     patch.object(analyzer,'model',return_value=None),patch('video_decode.FFmpegVideo',return_value=fallback), \
                     patch('nsfw_analysis.frame_result',return_value={'candidate':False,'status':'analyzed'}):
                    result=analyzer.video(self.root/'movie.mov',{'nsfw'})['nsfw']
                capture.read.assert_not_called();fallback.iter_samples.assert_called_once_with([0,2.5])
                self.assertEqual(result['status'],'analyzed' if recover else 'error')
                if recover:self.assertEqual(result['recovered_sample_seconds'],[0,2.5])

    def test_attached_picture_is_never_used_as_video_sample(self):
        import cv2
        import numpy as np
        from unittest.mock import Mock
        capture=Mock();capture.isOpened.return_value=True
        capture.get.side_effect={cv2.CAP_PROP_FPS:90000,cv2.CAP_PROP_FRAME_COUNT:3600000}.get
        fallback=Mock(fps=30,count=1200,duration=40)
        fallback.read.return_value=np.zeros((24,32,3),np.uint8)
        fallback.iter_samples.side_effect=lambda times:iter((s,fallback.read.return_value) for s in times)
        analyzer=Analyzer({})
        with patch('cv2.VideoCapture',return_value=capture),patch('video_decode.FFmpegVideo',return_value=fallback), \
             patch('nsfw_analysis.samples',return_value=[0,39]) as samples,patch.object(analyzer,'model',return_value=None), \
             patch('nsfw_analysis.frame_result',return_value={'candidate':False,'status':'analyzed'}):
            result=analyzer.video(self.root/'movie.wmv',{'nsfw'})['nsfw']
        samples.assert_called_once_with(40,30);capture.read.assert_not_called()
        fallback.iter_samples.assert_called_once_with([0,39]);self.assertEqual(result['status'],'analyzed')

    def test_repair_can_bypass_stalled_native_endpoint_probe(self):
        import cv2
        import numpy as np
        from unittest.mock import Mock
        capture=Mock();capture.get.return_value=0
        frame=np.zeros((24,32,3),np.uint8)
        fallback=Mock(fps=2,count=6,duration=3)
        fallback.iter_samples.side_effect=lambda times:iter((s,frame) for s in times)
        analyzer=Analyzer({'video_force_ffmpeg':True})
        with patch('cv2.VideoCapture',return_value=capture) as opened,patch('video_decode.endpoint_readable',side_effect=AssertionError('Native probe must be bypassed')),patch('video_decode.FFmpegVideo',return_value=fallback),patch('nsfw_analysis.samples',return_value=[0,2.5]),patch.object(analyzer,'model',return_value=None),patch('nsfw_analysis.frame_result',return_value={'candidate':False,'status':'analyzed'}):
            result=analyzer.video(self.root/'movie.mov',{'nsfw'})['nsfw']
        opened.assert_called_once_with();self.assertEqual(result['status'],'analyzed')

    def test_ffmpeg_fallback_excludes_cover_art_checks_decode_and_bounds_subprocess(self):
        from video_decode import FFmpegVideo
        from unittest.mock import Mock
        timeline=b'#tb 0: 1/30\n'+b''.join(f'0, {i}, {i}, 1, 100\n'.encode() for i in range(30))
        with patch('video_decode.subprocess.run',return_value=Mock(returncode=0,stderr=b'',stdout=timeline)) as run:
            reader=FFmpegVideo(self.root/'movie.wmv',{'ffmpeg':'ffmpeg.exe'})
        self.assertEqual(reader.count,30);self.assertEqual(reader.duration,1)
        self.assertIn('0:V:0',run.call_args.args[0]);self.assertIn('-xerror',run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['timeout'],180)
        for output in (Mock(returncode=1,stderr=b'decode failed',stdout=b''),
                       Mock(returncode=0,stderr=b'',stdout=b'frame=1\nout_time_us=1\nprogress=continue\n')):
            with patch('video_decode.subprocess.run',return_value=output),self.assertRaises(ValueError):
                FFmpegVideo(self.root/'movie.wmv',{'ffmpeg':'ffmpeg.exe'})

    def config(self):
        roots={name:self.root/name for name in ('onedrive','icloud','amazon')}
        # Root names resemble provider names, but the run folder stays outside them.
        for p in roots.values():p.mkdir()
        safe=[]
        for name in ('safe1.json','safe2.json'):
            p=self.root/name;p.write_text(json.dumps({'excluded_sha256':[]}));safe.append(p)
        deleted=self.root/'deleted.json';deleted.write_text(json.dumps({'excluded_import_sha256':[]}))
        receipts=self.root/'receipts.jsonl';receipts.write_text('')
        return {'roots':roots,'safe_ledgers':safe,'deletion_ledger':deleted,'delivery_ledger':receipts,'output':self.root/'runs'}
    def factory(self,config,checks,database,protected,deleted,timeout):
        processor=runner.FileProcessor(config,checks,database,protected,deleted,self.model)
        processor.process_row=processor.process;return processor
    def test_missing_selected_result_is_reported_as_gap_without_summary_crash(self):
        config=self.config()
        (config['roots']['onedrive']/'a.jpg').write_bytes(b'fixture')
        def factory(*args):
            obj=self.factory(*args)
            original=obj.process_row
            def missing(row):
                result=original(row)
                result['checks'].pop('junk',None)
                return result
            obj.process_row=missing
            return obj
        summary=runner.run(config,['quality','junk'],processor_factory=factory)
        self.assertEqual(summary['status'],'complete_with_gaps')
        self.assertEqual(summary['check_counts']['junk'],{'error':1})
        self.assertEqual(summary['candidate_counts']['junk'],0)
        coverage=config['output']/'pass'/'junk'/'coverage.jsonl'
        self.assertIn('Processor returned no result',coverage.read_text())

    def test_one_crawl_per_gallery_and_same_inventory_for_plans(self):
        config=self.config()
        for label in ('onedrive','icloud'):(config['roots'][label]/'a.jpg').write_bytes(b'fixture')
        (config['roots']['onedrive']/'a.jpg.xmp').write_bytes(b'tags')
        review=config['roots']['onedrive']/'FOR_REVIEW';review.mkdir();(review/'skip.jpg').write_bytes(b'skip')
        walk=os.walk
        with patch.object(runner.os,'walk',wraps=walk) as crawling:
            summary=runner.run(config,['duplicates','sync','quality'],processor_factory=self.factory)
        self.assertEqual(crawling.call_count,2);self.assertEqual(summary['processed'],3)
        self.assertEqual(summary['source_mutations'],0)
        out=config['output']/'pass'
        from two_library_duplicates import read_scan
        files=read_scan(out/'files.jsonl');self.assertEqual(len(files),2);self.assertTrue(all(r['kind']=='photo' for r in files))
        self.assertEqual(json.loads((out/'sync-plan.json').read_text())['counts']['planned'],0)
    def test_limited_run_holds_actions_and_blocks_legacy_scan_readers(self):
        config=self.config();(config['roots']['onedrive']/'a.jpg').write_bytes(b'fixture')
        (config['roots']['icloud']/'b.jpg').write_bytes(b'other')
        with patch('sync_local_photo_folders.settings.review_move_hashes',return_value=set()):
            summary=runner.run(config,['duplicates'],limit=1,processor_factory=self.factory)
        out=config['output']/'pass';self.assertEqual(summary['status'],'sample_complete')
        self.assertFalse(json.loads((out/'files.jsonl.complete.json').read_text())['complete'])
        from two_library_duplicates import read_scan
        with self.assertRaises(ValueError):read_scan(out/'files.jsonl')
        plan=json.loads((out/'duplicates-plan.json').read_text());self.assertTrue(all(r['hold'] for r in plan['copies']))
    def test_metadata_identified_live_movie_not_copied(self):
        config=self.config();p=config['roots']['onedrive']/'renamed.mov';p.write_bytes(b'fixture')
        with patch.object(self.model,'metadata',return_value={'status':'cataloged','embedded':{},'content_identifier':'APPLE-IDENTITY'}):
            runner.run(config,['duplicates','sync'],processor_factory=self.factory)
        for name in ('duplicates','sync'):
            data=json.loads((config['output']/'pass'/(name+'-plan.json')).read_text())
            self.assertEqual(data['copies'][0]['hold'],'live_photo_requires_complete_asset_plan')
    def test_missing_ledger_fails_before_crawl(self):
        config=self.config();config['safe_ledgers'][1].unlink()
        with patch.object(runner.os,'walk',side_effect=AssertionError('must not crawl')),self.assertRaises(FileNotFoundError):
            runner.run(config,['quality'],processor_factory=self.factory)

    def test_parallel_exact_aliases_infer_only_once(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        paths=[self.photo(str(n)+'.jpg') for n in range(4)]
        original=self.model.image
        def slow(path,checks):time.sleep(.1);return original(path,checks)
        self.model.image=slow
        def process(path):
            p=runner.FileProcessor({'require_media_metadata':True,'pass_file_timeout':2},['quality'],self.root/'parallel.sqlite',set(),set(),self.model)
            try:return p.process(row(path))
            finally:p.close()
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(process,paths))
        self.assertEqual(len(self.model.calls),2)
        self.assertEqual(sum(r['checks']['quality']['cache_reused'] for r in results),3)
        self.assertTrue(all(r['status']=='verified_local_bytes' for r in results))
    def test_windows_lease_reader_does_not_abandon_claim(self):
        path=self.photo();processor=self.processor(['quality'])
        unlink=Path.unlink;blocked=[]
        def transient(target,*args,**kwargs):
            if target.parent==processor.claims and target.suffix=='.json' and not blocked:
                blocked.append(str(target));raise PermissionError('Concurrent Windows reader')
            return unlink(target,*args,**kwargs)
        with patch.object(Path,'unlink',transient):result=processor.process(row(path))
        self.assertEqual(result['checks']['quality']['status'],'analyzed')
        self.assertEqual(len(blocked),1)
        self.assertFalse(list(processor.claims.glob('*.json')))

    def test_busy_exact_content_lease_stays_held(self):
        p=self.photo();processor=self.processor(['quality']);processor.config['pass_file_timeout']=.01
        digest=hashlib.sha256(p.read_bytes()).hexdigest()
        import os
        lease=processor.claims/(digest+'-'+str(p.stat().st_size)+'-photo.json');lease.write_text(json.dumps({'pid':os.getpid()}))
        result=processor.process(row(p));self.assertEqual(result['status'],'unreadable')
        self.assertTrue(lease.exists());self.assertFalse(self.model.calls)
    def test_real_parallel_workers_reuse_identity_cache(self):
        from PIL import Image
        p=self.root/'first.png';q=self.root/'second.png';Image.new('RGB',(96,96)).save(p);q.write_bytes(p.read_bytes())
        config={'pass_workers':2,'pass_file_timeout':30}
        processor=runner.ParallelProcessor(config,{'quality'},self.root/'workers.sqlite',set(),set(),30)
        try:results=list(processor.process_many([row(p),row(q)]))
        finally:processor.close()
        self.assertEqual(len(results),2)
        self.assertTrue(all(r['checks']['quality']['status']=='analyzed' for r in results))
        self.assertEqual(sum(r['checks']['quality']['cache_reused'] for r in results),1)
        self.assertFalse(list((self.root/'workers.claims').glob('*.json')))

if __name__=='__main__':unittest.main()
