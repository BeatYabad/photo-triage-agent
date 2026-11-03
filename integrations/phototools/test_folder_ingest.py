from collections import defaultdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import folder_ingest as ingest
import review_queue as queue


class IntakeTests(unittest.TestCase):
    def test_matching_still_bytes_do_not_merge_native_and_standalone(self):
        rows=[{'path':'/gallery/native.heic','sha256':'a'*64,'bytes':100,'native_live_photo':True},
              {'path':'/gallery/export.jpg','sha256':'a'*64,'bytes':100,'native_live_photo':False}]
        self.assertEqual(len(ingest.groups(rows)),2)

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.base=Path(temporary.name)
        roots={label:self.base/label for label in ('onedrive','icloud','amazon')}
        for root in roots.values():root.mkdir()
        self.folder=roots['onedrive']/'intake';self.folder.mkdir();self.out=self.base/'runs'/'intake';self.out.mkdir(parents=True)
        safe=[]
        for number in (1,2):
            path=self.base/f'safe-{number}.json';path.write_text('{"excluded_sha256":[]}');safe.append(path)
        deleted=self.base/'deleted.json';deleted.write_text('{"excluded_import_sha256":[]}')
        self.config={'roots':roots,'safe_ledgers':safe,'deletion_ledger':deleted,'delivery_ledger':self.base/'delivery.jsonl',
            'output':self.base/'runs','preservation':self.base/'backup','review_state':self.base/'state'/'review-state.json',
            'review_folder':self.base/'processed'/'FOR_REVIEW','nsfw_review_folder':self.base/'processed'/'NSFW',
            'nsfw_confirmed_folder':self.base/'processed'/'NSFW'/'CONFIRMED','restore_folder':self.base/'processed'/'RESTORE'}
        self.config['review_state'].parent.mkdir();queue.save_state(self.config['review_state'],{'version':1,'items':{}})
        self.index=defaultdict(list);self.unavailable=defaultdict(set);self.recycled=[]
        for target in (patch.object(queue,'fresh',side_effect=queue.fingerprint),patch('sync_local_photo_folders.media.space'),
            patch.dict('os.environ',{'PHOTOS_CONFIG':''}),patch.object(ingest,'ensure_digikam_closed'),
            patch.object(ingest.nsfw_review,'ensure_digikam_closed'),
            patch.object(ingest,'target_index',side_effect=lambda *args:(self.index,self.unavailable)),
            patch.object(ingest.nsfw_review,'service',return_value={'held':[]})):
            target.start();self.addCleanup(target.stop)

    def row(self,name='20200102_photo.jpg',content=b'fixture',nsfw=False,junk=False):
        path=self.folder/name;path.write_bytes(content)
        return {'path':str(path),'root':str(self.config['roots']['onedrive']),'label':'onedrive','library':'onedrive',
            'sha256':hashlib.sha256(content).hexdigest(),'bytes':len(content),'mtime_ns':path.stat().st_mtime_ns,
            'status':'verified_local_bytes','kind':'video' if path.suffix.lower() in {'.mp4','.mov'} else 'photo',
            'checks':{check:{'status':'analyzed','candidate':candidate,'rule':'unified-pass-v1-fixture'} for check,candidate in (('nsfw',nsfw),('junk',junk))}}

    def recycle(self,path):self.recycled.append(path);Path(path).unlink()
    def finish(self,rows,apply=True):return ingest.finish(self.config,self.folder,self.out,rows,apply,self.recycle)

    def test_junk_stays_in_place_without_explicit_removal_approval(self):
        row=self.row(junk=True);result=self.finish([row])
        self.assertTrue(Path(row['path']).exists());self.assertFalse(self.recycled)
        self.assertEqual(result['actions']['junk_reported_left_in_place'],1)

    def test_filter_nomination_propagates_across_live_photo_pair(self):
        still=self.row('IMG_1.jpg',nsfw=True);motion=self.row('IMG_1.mov',b'motion')
        group=ingest.groups([still,motion]);self.assertEqual(len(group),1)
        self.assertEqual(ingest.classify(group[0],{'items':{}},{'items':{}},set(),set())[0],'nsfw')

    def test_safe_member_conflict_preserves_whole_bundle(self):
        still=self.row('IMG_1.jpg',nsfw=True);motion=self.row('IMG_1.mov',b'motion')
        self.assertEqual(ingest.classify([still,motion],{'items':{}},{'items':{}},{motion['sha256']},set())[0],'held')

    def test_nsfw_moves_complete_pair_to_review_with_exact_restore_origins(self):
        still=self.row('IMG_1.jpg',nsfw=True);motion=self.row('IMG_1.mov',b'motion')
        result=self.finish([still,motion]);self.assertFalse(result['held'])
        self.assertEqual(result['actions']['moved_nsfw'],2)
        for row in (still,motion):
            item=queue.load_state(self.config['review_state'])['items'][row['sha256']]
            self.assertEqual(item['origins'][0]['path'],row['path'])
            self.assertEqual(item['status'],'reviewing');self.assertFalse(Path(row['path']).exists())
            self.assertTrue(Path(item['review_path']).exists())

    def test_accepted_standalone_preserves_name_sorts_and_imports(self):
        row=self.row();result=self.finish([row])
        self.assertFalse(result['held']);self.assertFalse(Path(row['path']).exists())
        self.assertEqual((self.config['roots']['onedrive']/'2020'/'01'/Path(row['path']).name).read_bytes(),b'fixture')
        self.assertEqual((self.config['roots']['icloud']/Path(row['path']).name).read_bytes(),b'fixture')
        self.assertFalse(result['cloud_sync_verified'])

    def test_missing_live_asset_is_never_imported_as_separate_still_and_video(self):
        still=self.row('20200102.jpg');motion=self.row('20200102.mov',b'motion')
        result=self.finish([still,motion])
        self.assertEqual(len(result['held']),2);self.assertFalse(list(self.config['roots']['icloud'].iterdir()))
        self.assertEqual(result['actions']['organized_onedrive'],2)

    def test_exact_duplicate_intake_goes_to_dupes_and_retains_keeper(self):
        row=self.row();keeper=self.config['roots']['onedrive']/'2020'/'01'/'existing.jpg';keeper.parent.mkdir(parents=True);keeper.write_bytes(b'fixture')
        self.index[('onedrive',row['sha256'],row['bytes'])].append(str(keeper))
        result=self.finish([row]);self.assertFalse(result['held'])
        self.assertEqual(result['actions']['duplicates_to_review'],1);self.assertTrue(keeper.exists())
        self.assertEqual(len(list((self.config['review_folder']/'DUPES').iterdir())),1)

    def test_unavailable_cloud_comparison_never_creates_extra_import(self):
        row=self.row();self.unavailable['icloud'].add(row['bytes'])
        result=self.finish([row]);self.assertTrue(result['held']);self.assertFalse(list(self.config['roots']['icloud'].iterdir()))

    def test_model_nomination_is_never_a_human_confirmation(self):
        row=self.row(nsfw=True);self.finish([row])
        item=queue.load_state(self.config['review_state'])['items'][row['sha256']]
        self.assertNotIn('nsfw_confirmation',item)

    def test_existing_human_confirmation_routes_returned_copies_without_new_approval(self):
        row=self.row(nsfw=True);self.finish([row])
        item=queue.load_state(self.config['review_state'])['items'][row['sha256']]
        self.config['nsfw_confirmed_folder'].mkdir(parents=True)
        Path(item['review_path']).rename(self.config['nsfw_confirmed_folder']/'approved.jpg')
        ingest.nsfw_review.process(self.config,apply=True)
        alias=self.row('returned.jpg')
        result=self.finish([alias]);self.assertFalse(result['held'])
        self.assertEqual(result['actions']['already_confirmed_to_privacy_flow'],1)
        self.assertFalse(Path(alias['path']).exists())

    def test_renamed_identical_inputs_are_one_content_group(self):
        first=self.row('one.jpg');second=self.row('two.jpg')
        self.assertEqual(len(ingest.groups([first,second])),1)

    def test_resume_verifies_receipts_without_repeating_moves(self):
        row=self.row(nsfw=True);self.finish([row]);result=self.finish([row])
        self.assertFalse(result['held']);self.assertEqual(result['resumed_verified'],1)
        self.assertEqual(result['actions']['moved_nsfw'],1);self.assertEqual(len(self.recycled),1)

    def test_resume_rejects_damaged_review_destination(self):
        row=self.row(nsfw=True);self.finish([row])
        from expand_reference_selections import read_jsonl
        receipt=list(read_jsonl(self.out/'actions.jsonl'))[-1];Path(receipt['destination']).write_bytes(b'changed')
        result=self.finish([row]);self.assertTrue(result['held']);self.assertEqual(result['resumed_verified'],0)

    def test_embedded_index_without_association_does_not_invent_live_photo(self):
        row=self.row();row['metadata']={'live_photo_video_index':123}
        result=self.finish([row]);self.assertFalse(Path(row['path']).exists())
        self.assertTrue(list(self.config['roots']['icloud'].iterdir()));self.assertEqual(result['actions']['organized_onedrive'],1)

    def test_explicit_private_routing_overrides_deletion_skip_and_moves_companion(self):
        from privacy_routes import register
        still=self.row('IMG_1.jpg');motion=self.row('IMG_1.mov',b'motion')
        self.config['deletion_ledger'].write_text(json.dumps({'excluded_import_sha256':[still['sha256']]}))
        register(self.config,[still,motion],{still['sha256']})
        result=self.finish([still,motion]);self.assertFalse(result['held']);self.assertEqual(result['actions']['moved_nsfw'],2)
        self.assertFalse(Path(motion['path']).exists())

    def test_old_unconfirmed_nomination_does_not_recreate_handled_review(self):
        row=self.row(nsfw=True);self.finish([row]);state=queue.load_state(self.config['review_state'])
        item=state['items'][row['sha256']];Path(item['review_path']).unlink();item['review_copy_handled_by_user']=True
        queue.save_state(self.config['review_state'],state)
        alias=self.row('returned.jpg');result=self.finish([alias])
        self.assertTrue(result['held']);self.assertTrue(Path(alias['path']).exists());self.assertFalse(Path(item['review_path']).exists())

    def test_explicit_private_route_still_routes_after_review_was_handled(self):
        from privacy_routes import register
        row=self.row(nsfw=True);register(self.config,[row],{row['sha256']})
        self.finish([row]);state=queue.load_state(self.config['review_state'])
        item=state['items'][row['sha256']];Path(item['review_path']).unlink();item['review_copy_handled_by_user']=True
        queue.save_state(self.config['review_state'],state)
        alias=self.row('returned.jpg');result=self.finish([alias])
        self.assertFalse(result['held']);self.assertFalse(Path(alias['path']).exists());self.assertTrue(Path(item['review_path']).exists())

    def test_fresh_nsfw_evidence_routes_returned_unconfirmed_copy(self):
        row=self.row(nsfw=True);self.finish([row]);state=queue.load_state(self.config['review_state'])
        item=state['items'][row['sha256']];Path(item['review_path']).unlink();item['review_copy_handled_by_user']=True
        queue.save_state(self.config['review_state'],state)
        alias=self.row('returned.jpg',nsfw=True);result=self.finish([alias])
        self.assertFalse(result['held']);self.assertFalse(Path(alias['path']).exists())
        self.assertTrue(Path(item['review_path']).exists())
        self.assertNotIn('nsfw_confirmation',queue.load_state(self.config['review_state'])['items'][row['sha256']])

    def test_unrelated_deletion_is_not_mislabeled_as_junk_or_nsfw(self):
        row=self.row();kind,reason=ingest.classify([row],{'items':{}},{'items':{}},set(),{row['sha256']})
        self.assertEqual(kind,'held');self.assertIn('deletion',reason)

    def test_absent_private_companion_does_not_leave_present_copy_exposed(self):
        from privacy_routes import register
        still=self.row('IMG_1.jpg');motion=self.row('IMG_1.mov',b'motion')
        register(self.config,[still,motion],{still['sha256'],motion['sha256']})
        Path(motion['path']).unlink();result=self.finish([still,motion])
        self.assertFalse(Path(still['path']).exists());self.assertEqual(result['actions']['moved_nsfw'],1)
        self.assertEqual(len(result['held']),1);self.assertIn('absent',result['held'][0]['error'])

    def test_known_private_identity_bypasses_model_and_deletion_skip(self):
        from privacy_routes import register
        from photo_pass import FileProcessor
        from test_photo_pass import FakeAnalyzer
        row=self.row();register(self.config,[row],{row['sha256']});model=FakeAnalyzer()
        processor=FileProcessor(self.config,{'nsfw','junk'},self.out/'cache.sqlite',set(),{row['sha256']},model)
        self.addCleanup(processor.close)
        result=processor.process({**row,'placeholder':False})
        self.assertFalse(model.calls);self.assertTrue(result['checks']['nsfw']['candidate'])
        self.assertEqual(result['checks']['nsfw']['rule'],'unified-pass-v1-private-route')

    def test_shared_gallery_routing_moves_private_copies_in_both_libraries(self):
        from privacy_routes import route,register
        first=self.row();second={**first,'path':str(self.config['roots']['icloud']/'renamed.jpg'),
            'label':'icloud','library':'icloud','root':str(self.config['roots']['icloud'])}
        Path(second['path']).write_bytes(b'fixture');second['mtime_ns']=Path(second['path']).stat().st_mtime_ns
        register(self.config,[first],{first['sha256']})
        result=route(self.config,[first,second],self.out/'routing',True,self.recycle)
        self.assertFalse(result['held']);self.assertEqual(result['moved_files'],2)
        self.assertFalse(Path(first['path']).exists());self.assertFalse(Path(second['path']).exists())
        item=queue.load_state(self.config['review_state'])['items'][first['sha256']]
        self.assertEqual(item['representative']['label'],'onedrive');self.assertEqual(len(item['origins']),2)

    def test_missing_private_policy_fails_closed(self):
        from privacy_routes import register,hashes,location
        row=self.row();register(self.config,[row],{row['sha256']})
        location(self.config).unlink()
        with self.assertRaisesRegex(ValueError,'routing ledger missing'):hashes(self.config)


if __name__=='__main__':unittest.main()
