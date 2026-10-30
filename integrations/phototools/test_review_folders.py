import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import review_deletions as deletion
import review_routes as routes
import review_queue as queue
import photo_settings as settings


class ReviewFolderTests(unittest.TestCase):
    def test_new_triage_image_and_video_findings_move_to_review(self):
        from photo_triage.vision import junk_result
        from video_filter_review import aggregate
        for category,evidence_type in [('meme_or_cartoon','humorous_caption'),('screenshot','captured_ui'),('receipt','transaction_details')]:
            for video in (False,True):
                with self.subTest(category=category,video=video):
                    row=self.file(category+str(video)+('.mp4' if video else '.jpg'),(category+str(video)).encode())
                    finding=junk_result({'source':'photo_triage_agent','category':category,
                        'evidence_type':evidence_type,'observation':'Concrete visible category evidence'},[100,100])
                    row['kind']='video' if video else 'photo'
                    row['checks']['junk']=aggregate('junk',[finding],1,1) if video else finding
                    result=routes.route(self.config,[row],self.out,True,self.recycler)
                    self.assertEqual(result['moved_files'],1,result)
                    self.assertFalse(result['held'],result)
                    self.assertFalse(Path(row['path']).exists())

    def test_triage_routing_rejects_mismatched_or_incomplete_evidence(self):
        from photo_triage.vision import junk_result
        from video_filter_review import aggregate
        finding=junk_result({'source':'photo_triage_agent','category':'meme_or_cartoon',
            'evidence_type':'humorous_caption','observation':'A captioned joke'},[100,100])
        self.assertFalse(routes.supported_triage_finding({**finding,'category':'receipt'}))
        self.assertFalse(routes.supported_triage_finding({**finding,'contextual_evidence':{}}))
        self.assertFalse(routes.supported_triage_finding(aggregate('junk',[finding],2,2)))

    def test_empty_regular_files_are_exact_delete_requests(self):
        a=self.file('empty.txt',b'');b=self.file('renamed.bin',b'','icloud')
        (self.config['delete_folder']/'empty.txt').write_bytes(b'')
        result=deletion.service(self.config,self.recycler)
        self.assertFalse(result['held'],result)
        self.assertEqual(result['removed_gallery_files'],2)
        self.assertFalse(Path(a['path']).exists());self.assertFalse(Path(b['path']).exists())
        self.assertEqual(deletion.intents(self.config)['items'][a['sha256']]['status'],'deleted_locally_verified')

    def test_delete_nonmedia_matches_both_galleries_by_bytes_not_name(self):
        a=self.file('document.bin',b'approved')
        b=self.file('renamed.dat',b'approved','icloud')
        keep=self.file('document.bin',b'different','icloud')
        (self.config['delete_folder']/'decision.dat').write_bytes(b'approved')
        result=deletion.service(self.config,self.recycler)
        self.assertFalse(result['held'],result)
        self.assertEqual(result['removed_gallery_files'],2)
        self.assertFalse(Path(a['path']).exists());self.assertFalse(Path(b['path']).exists())
        self.assertTrue(Path(keep['path']).exists())

    def test_partial_gallery_delete_stays_pending_and_retains_request(self):
        a=self.file('a.jpg');b=self.file('b.jpg',library='icloud')
        inbox=self.config['delete_folder']/'approved.jpg';inbox.write_bytes(b'photo')
        requests={'version':1,'items':{a['sha256']:{'bytes':a['bytes'],'status':'pending','inbox_paths':[str(inbox)]}}}
        result=deletion.execute({**self.config,'delete_galleries':['onedrive']},requests,[a],self.recycler)
        self.assertTrue(result['held']);self.assertFalse(Path(a['path']).exists())
        self.assertTrue(Path(b['path']).exists());self.assertTrue(inbox.exists())
        self.assertEqual(deletion.intents(self.config)['items'][a['sha256']]['status'],'pending')
        result=deletion.service(self.config,self.recycler)
        self.assertFalse(result['held'],result);self.assertFalse(Path(b['path']).exists())
        self.assertFalse(inbox.exists())

    def test_delete_batch_hashes_same_size_unrelated_candidate_once(self):
        rows=[self.file('a.jpg',b'aaaa'),self.file('b.jpg',b'bbbb','icloud'),self.file('keep.jpg',b'cccc')]
        requests={'version':1,'items':{r['sha256']:{'bytes':r['bytes'],'status':'pending','inbox_paths':[]}
                                     for r in rows[:2]}}
        with patch.object(queue,'fresh',side_effect=queue.fingerprint) as fresh:
            result=deletion.execute(self.config,requests,rows,self.recycler)
        self.assertFalse(result['held'],result)
        self.assertEqual(result['removed_gallery_files'],2)
        self.assertFalse(self.config['preservation'].exists())
        self.assertEqual(sum(str(c.args[0])==rows[2]['path'] for c in fresh.call_args_list),1)
        self.assertEqual(Path(rows[2]['path']).read_bytes(),b'cccc')

    def test_indexed_delete_still_rechecks_bytes_at_removal(self):
        row=self.file('a.jpg',b'aaaa')
        requests={'version':1,'items':{row['sha256']:{'bytes':4,'status':'pending','inbox_paths':[]}}}
        calls=0
        def change_after_index(path):
            nonlocal calls
            actual=queue.fingerprint(path);calls+=1
            if calls==1:Path(row['path']).write_bytes(b'new!')
            return actual
        with patch.object(queue,'fresh',side_effect=change_after_index):
            result=deletion.execute(self.config,requests,[row],self.recycler)
        self.assertTrue(result['held'])
        self.assertEqual(Path(row['path']).read_bytes(),b'new!')
        self.assertEqual(result['removed_gallery_files'],0)

    def test_restore_import_subtree_with_parent_selected(self):
        root=self.base/'intake';root.mkdir()
        nested=root/'nested';nested.mkdir()
        source=self.config['restore_folder']/'restored.jpg';source.write_bytes(b'photo')
        actual={'path':str(source),**queue.fingerprint(source)}
        original={'label':'import','root':str(nested),'path':str(nested/'original.jpg'),
                  'sha256':actual['sha256'],'bytes':actual['bytes']}
        targets=queue.restore_recorded_locations({'import':root},actual,{'origins':[original]},self.out/'restore.jsonl')
        self.assertEqual(targets,[nested/'original.jpg'])
        self.assertEqual(targets[0].read_bytes(),b'photo')
        self.assertFalse(self.config['preservation'].exists())

    def test_restore_rejects_unrelated_import_root(self):
        root=self.base/'intake';root.mkdir()
        outside=self.base/'outside';outside.mkdir()
        source=self.config['restore_folder']/'restored.jpg';source.write_bytes(b'photo')
        actual={'path':str(source),**queue.fingerprint(source)}
        original={'label':'import','root':str(outside),'path':str(outside/'original.jpg'),
                  'sha256':actual['sha256'],'bytes':actual['bytes']}
        with self.assertRaisesRegex(ValueError,'root differs'):
            queue.restore_recorded_locations({'import':root},actual,{'origins':[original]},self.out/'restore.jsonl')
        self.assertFalse((outside/'original.jpg').exists())

    def test_current_inbox_scope_does_not_replay_absent_requests(self):
        old=self.file('old.jpg',b'old!')
        current=self.file('current.jpg',b'new!')
        deletion.write_json(self.config['review_state'].parent/'delete-requests.json',
            {'version':1,'items':{old['sha256']:{'bytes':4,'status':'pending','inbox_paths':[]}}})
        (self.config['delete_folder']/'approved.jpg').write_bytes(b'new!')
        result=deletion.service({**self.config,'review_delete_current_inbox_only':True},self.recycler)
        self.assertFalse(result['held'])
        self.assertTrue(Path(old['path']).exists())
        self.assertFalse(Path(current['path']).exists())
        self.assertEqual(result['cleared_delete_files'],1)
        self.assertIn(old['sha256'],deletion.intents(self.config)['items'])

    def test_delete_accepts_quicktime_extension(self):
        (self.config['delete_folder']/'motion.jpeg.qt').write_bytes(b'motion')
        result=deletion.service({**self.config,'review_delete_current_inbox_only':True},self.recycler)
        self.assertFalse(result['held'])
        self.assertEqual(result['cleared_delete_files'],1)
        self.assertFalse(self.config['preservation'].exists())

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        roots={key:self.base/key for key in ('onedrive','icloud','amazon')}
        for root in roots.values():root.mkdir()
        safe=[]
        for number in range(2):
            path=self.base/f'safe{number}.json';path.write_text('{"excluded_sha256":[]}');safe.append(path)
        deleted=self.base/'deletions.json';deleted.write_text('{"excluded_import_sha256":[]}')
        self.config={'roots':roots,'safe_ledgers':safe,'deletion_ledger':deleted,
            'review_state':self.base/'state'/'review-state.json','review_folder':self.base/'FOR_REVIEW',
            'restore_folder':self.base/'RESTORE','delete_folder':self.base/'DELETE',
            'nsfw_review_folder':self.base/'NSFW','nsfw_confirmed_folder':self.base/'NSFW'/'CONFIRMED',
            'preservation':self.base/'preservation','output':self.base/'reports'}
        for key in ('review_folder','restore_folder','delete_folder','output'):self.config[key].mkdir()
        self.config['review_state'].parent.mkdir()
        self.out=self.base/'run';self.out.mkdir()
        for target in (patch.object(queue,'fresh',side_effect=queue.fingerprint),
                       patch('folder_ingest.ensure_digikam_closed'),patch('windows_process_check.ensure_digikam_closed'),
                       patch.dict('os.environ',{},clear=True)):
            target.start();self.addCleanup(target.stop)
        self.recycler=lambda path:Path(path).unlink()

    def file(self,name,data=b'photo',library='onedrive'):
        path=self.config['roots'][library]/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        return {'path':str(path),'label':library,'library':library,'root':str(self.config['roots'][library]),
                'kind':'photo','status':'verified_local_bytes',**queue.fingerprint(path),
                'checks':{'junk':{'status':'analyzed','candidate':True,'category':'meme_or_cartoon',
                                 'rule':'unified-pass-v1-fixture','policy':'supported-junk-v1',
                                 'confidence_band':'supported_review'},'nsfw':{'status':'analyzed','candidate':False}}}

    def moved(self):
        rows=[self.file('original.jpg'),self.file('other-name.jpg',library='icloud')]
        result=routes.route(self.config,rows,self.out,True,self.recycler,verify=True)
        return rows,result

    def restore(self):
        with queue.locked(self.config['review_state']):
            return queue.restore_files(self.config['roots'],self.config['review_state'],self.config['restore_folder'],
                self.config['review_state'].parent/'.staging',self.config['review_state'].parent/'review-actions.jsonl')

    def test_review_moves_both_aliases_with_one_personal_named_representative(self):
        with patch('photo_pass.inventory',side_effect=AssertionError('No new crawl')):
            rows,result=self.moved()
        self.assertEqual(result['moved_files'],2)
        self.assertEqual([p.name for p in self.config['review_folder'].rglob('*') if p.is_file()],['original.jpg'])
        self.assertFalse(any(Path(row['path']).exists() for row in rows))
        item=queue.load_state(self.config['review_state'])['items'][rows[0]['sha256']]
        self.assertEqual(len(item['origins']),2);self.assertEqual(item['status'],'reviewing')

    def test_returned_copy_joins_existing_review_without_second_nomination(self):
        self.moved()
        returned=self.file('returned.jpg',library='icloud')
        returned['checks']={}
        result=routes.route(self.config,[returned],self.out,True,self.recycler)
        self.assertEqual(result['moved_files'],1,result)
        self.assertEqual(len([p for p in self.config['review_folder'].rglob('*') if p.is_file()]),1)
        self.assertFalse(Path(returned['path']).exists())

    def test_missing_review_copy_is_hold_not_deletion_or_new_review(self):
        rows,_=self.moved()
        state=queue.load_state(self.config['review_state'])
        Path(state['items'][rows[0]['sha256']]['review_path']).unlink()
        returned=self.file('returned.jpg',library='icloud');returned['checks']={}
        result=routes.route(self.config,[returned],self.out,True,self.recycler)
        self.assertTrue(result['held'])
        self.assertTrue(Path(returned['path']).exists())

    def test_completed_delete_catches_phone_reupload_on_next_service(self):
        self.file('one.jpg')
        (self.config['delete_folder']/'delete.jpg').write_bytes(b'photo')
        first=deletion.service(self.config,self.recycler)
        self.assertFalse(first['held'])
        returned=self.file('returned.jpg',library='icloud')
        second=deletion.service(self.config,self.recycler)
        self.assertEqual(second['removed_gallery_files'],1,second)
        self.assertFalse(Path(returned['path']).exists())

    def test_restore_returns_both_exact_original_locations_and_clears_inbox(self):
        rows,_=self.moved()
        (self.config['review_folder']/'Memes and cartoons'/'original.jpg').rename(self.config['restore_folder']/'renamed.jpg')
        result=self.restore()
        self.assertFalse(result['held']);self.assertEqual(result['restore_files_cleared'],1)
        self.assertTrue(all(Path(row['path']).read_bytes()==b'photo' for row in rows))
        self.assertEqual(queue.load_state(self.config['review_state'])['items'][rows[0]['sha256']]['status'],'kept')

    def test_unknown_restore_goes_to_dated_camera_roll_without_invented_origins(self):
        path=self.config['restore_folder']/'20250102_unknown.jpg';path.write_bytes(b'unknown')
        result=self.restore()
        self.assertFalse(result['held']);self.assertFalse(path.exists())
        self.assertEqual((self.config['roots']['onedrive']/'2025'/'01'/path.name).read_bytes(),b'unknown')
        item=next(iter(queue.load_state(self.config['review_state'])['items'].values()))
        self.assertEqual(item['origins'],[]);self.assertEqual(item['origin_kind'],'unknown_review')

    def test_occupied_restore_location_never_overwritten_or_cleared(self):
        rows,_=self.moved();Path(rows[0]['path']).write_bytes(b'other')
        path=self.config['restore_folder']/'wanted.jpg'
        (self.config['review_folder']/'Memes and cartoons'/'original.jpg').rename(path)
        result=self.restore()
        self.assertTrue(result['held']);self.assertTrue(path.exists());self.assertEqual(Path(rows[0]['path']).read_bytes(),b'other')

    def test_wanted_and_changed_candidates_never_move(self):
        row=self.file('wanted.jpg');self.config['safe_ledgers'][0].write_text(json.dumps({'excluded_sha256':[row['sha256']]}))
        self.assertEqual(routes.route(self.config,[row],self.out,True,self.recycler)['moved_files'],0)
        self.config['safe_ledgers'][0].write_text('{"excluded_sha256":[]}');Path(row['path']).write_bytes(b'changed')
        result=routes.route(self.config,[row],self.out,True,self.recycler)
        self.assertTrue(result['held']);self.assertTrue(Path(row['path']).exists())

    def test_old_weak_junk_candidate_cannot_be_moved_by_routing(self):
        row=self.file('weak.jpg');row['checks']['junk'].pop('policy')
        result=routes.route(self.config,[row],self.out,True,self.recycler)
        self.assertEqual(result['moved_files'],0);self.assertTrue(result['held'])
        self.assertTrue(Path(row['path']).exists())

    def test_nested_delete_files_remove_exact_matches_and_clear_all_copies(self):
        row=self.file('personal.jpg')
        other=self.file('unrelated.jpg',b'keep this')
        paths=[self.config['delete_folder']/'Memes'/'remove.jpg',
               self.config['delete_folder']/'Receipts'/'nested'/'another-name.jpg']
        for path in paths:
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'photo')
        result=deletion.service({**self.config,'delete_galleries':['onedrive']},self.recycler)
        self.assertFalse(result['held'])
        self.assertEqual(result['removed_gallery_files'],1)
        self.assertEqual(result['cleared_delete_files'],2)
        self.assertFalse(any(path.exists() for path in paths))
        self.assertFalse(Path(row['path']).exists())
        self.assertTrue(Path(other['path']).exists())

    def test_standalone_native_evidence_releases_false_bundle_hold(self):
        row=self.file('standalone.png',library='icloud');row['native_live_photo']=True
        inbox=self.config['delete_folder']/'remove.png';inbox.write_bytes(b'photo')
        config={**self.config,'icloud_database':self.base/'database.sqlite','delete_galleries':['icloud']}
        with patch('live_photo_cleanup.native_assets',return_value={}),patch('photo_pass.inventory',return_value=([row],[])),patch('icloud_asset_record.standalone_record',return_value={'resource_key':'resOriginalRes'}):
            result=deletion.service(config,self.recycler)
        self.assertFalse(result['held']);self.assertFalse(inbox.exists());self.assertFalse(Path(row['path']).exists())

    def test_real_native_bundle_stays_held(self):
        row=self.file('native.heic',library='icloud');row['native_live_photo']=True
        inbox=self.config['delete_folder']/'remove.heic';inbox.write_bytes(b'photo')
        config={**self.config,'icloud_database':self.base/'database.sqlite','delete_galleries':['icloud']}
        with patch('live_photo_cleanup.native_assets',return_value={}),patch('photo_pass.inventory',return_value=([row],[])),patch('icloud_asset_record.standalone_record',side_effect=ValueError('Unpreserved motion')):
            result=deletion.service(config,self.recycler)
        self.assertTrue(result['held']);self.assertTrue(inbox.exists());self.assertTrue(Path(row['path']).exists())

    def test_prior_review_verifies_after_explicit_restore(self):
        self.moved()
        source=next(p for p in self.config['review_folder'].rglob('*') if p.is_file())
        source.rename(self.config['restore_folder']/'wanted.jpg')
        self.assertFalse(self.restore()['held'])
        result=routes.verify_moves(self.config,self.out)
        self.assertFalse(result['held']);self.assertEqual(result['verified_receipts'],2)

    def test_prior_review_verifies_after_explicit_delete(self):
        self.moved()
        source=next(p for p in self.config['review_folder'].rglob('*') if p.is_file())
        source.rename(self.config['delete_folder']/'remove.jpg')
        self.assertFalse(deletion.service(self.config,self.recycler)['held'])
        result=routes.verify_moves(self.config,self.out)
        self.assertFalse(result['held']);self.assertEqual(result['verified_receipts'],2)

    def test_known_icloud_restore_needs_no_onedrive_root(self):
        row=self.file('known.jpg',library='icloud')
        result=routes.route(self.config,[row],self.out,True,self.recycler)
        self.assertEqual(result['moved_files'],1)
        target=next(p for p in self.config['review_folder'].rglob('*') if p.is_file())
        target.rename(self.config['restore_folder']/'wanted.jpg')
        with queue.locked(self.config['review_state']):
            result=queue.restore_files({'icloud':self.config['roots']['icloud']},self.config['review_state'],
                self.config['restore_folder'],self.config['review_state'].parent/'.staging',
                self.config['review_state'].parent/'review-actions.jsonl')
        self.assertFalse(result['held']);self.assertEqual(result['restore_files_cleared'],1)
        self.assertEqual(Path(row['path']).read_bytes(),b'photo')

    def test_onedrive_task_delete_mirrors_to_apple(self):
        personal=self.file('personal.jpg');apple=self.file('apple.jpg',library='icloud')
        (self.config['delete_folder']/'remove.jpg').write_bytes(b'photo')
        from photo_pass import inventory
        with patch('photo_pass.inventory',wraps=inventory) as crawl:
            result=deletion.service({**self.config,'active_galleries':['onedrive']},self.recycler)
        self.assertEqual(set(crawl.call_args.args[0]),{'onedrive','icloud'})
        self.assertFalse(Path(personal['path']).exists());self.assertFalse(Path(apple['path']).exists())
        self.assertEqual(result['cleared_delete_files'],1)
        self.assertEqual(next(iter(deletion.intents(self.config)['items'].values()))['deferred_galleries'],[])

    def test_delete_scope_overrides_onedrive_only_cleanup(self):
        rows=[self.file('personal.jpg'),self.file('apple.jpg',library='icloud')]
        (self.config['delete_folder']/'remove.jpg').write_bytes(b'photo')
        result=deletion.service({**self.config,'active_galleries':['onedrive'],'delete_galleries':['onedrive','icloud']},self.recycler)
        self.assertEqual(result['removed_gallery_files'],2)
        self.assertFalse(any(Path(r['path']).exists() for r in rows))
        self.assertEqual(result['gallery_crawls'],2)
        self.assertEqual(result['cleared_delete_files'],1)

    def test_delete_freshly_finds_all_exact_matches_in_both_galleries(self):
        rows=[self.file('one.jpg'),self.file('two.jpg'),self.file('cloud.jpg',library='icloud')]
        unrelated=self.file('one.jpg',b'other','icloud')
        path=self.config['delete_folder']/'renamed.jpg';path.write_bytes(b'photo')
        result=deletion.service(self.config,self.recycler)
        self.assertFalse(result['held']);self.assertEqual(result['removed_gallery_files'],3)
        self.assertFalse(path.exists());self.assertFalse(any(Path(r['path']).exists() for r in rows))
        self.assertTrue(Path(unrelated['path']).exists())
        ledger=json.loads(self.config['deletion_ledger'].read_text())
        self.assertIn(rows[0]['sha256'],ledger['folder_deleted_sha256'])

    def test_unrelated_unavailable_inventory_file_does_not_block_delete(self):
        row=self.file('one.jpg')
        cloud=self.file('unavailable.mov',b'other-sized-bytes',library='icloud')
        path=self.config['delete_folder']/'remove.jpg';path.write_bytes(b'photo')
        with patch('photo_pass.inventory',return_value=([row],[{'path':cloud['path'],'error':'Nonresident placeholder'}])):
            result=deletion.service(self.config,self.recycler)
        self.assertFalse(result['held']);self.assertFalse(path.exists())
        self.assertFalse(Path(row['path']).exists());self.assertTrue(Path(cloud['path']).exists())

    def test_delete_retains_inbox_while_possible_matching_cloud_bytes_unavailable(self):
        row=self.file('one.jpg');cloud=self.file('cloud.jpg',library='icloud')
        path=self.config['delete_folder']/'remove.jpg';path.write_bytes(b'photo')
        original=queue.fingerprint
        def read(path):
            if str(path)==cloud['path']:raise ValueError('Cloud original unavailable')
            return original(path)
        with patch.object(queue,'fresh',side_effect=read):result=deletion.service(self.config,self.recycler)
        self.assertTrue(result['held']);self.assertTrue(path.exists());self.assertTrue(Path(cloud['path']).exists())

    def test_restore_conflict_prevents_delete_and_remains_visible(self):
        row=self.file('one.jpg')
        for key in ('restore_folder','delete_folder'):(self.config[key]/'photo.jpg').write_bytes(b'photo')
        result=deletion.service(self.config,self.recycler)
        self.assertTrue(result['held']);self.assertEqual(result['removed_gallery_files'],0)
        self.assertTrue(Path(row['path']).exists())

    def test_delete_overrides_older_safe_decision_and_returned_copy_is_removed(self):
        row=self.file('one.jpg');digest=row['sha256']
        self.config['safe_ledgers'][0].write_text(json.dumps({'excluded_sha256':[digest]}))
        (self.config['delete_folder']/'photo.jpg').write_bytes(b'photo')
        deletion.service(self.config,self.recycler)
        with patch.dict('os.environ',{'PHOTOS_CONFIG':'fixture'}),patch.object(settings,'load',return_value=self.config):
            self.assertNotIn(digest,settings.hashes(self.config['safe_ledgers']))
        returned=self.file('returned.jpg')
        result=deletion.execute(self.config,deletion.intents(self.config),[returned],self.recycler,include_completed=True)
        self.assertEqual(result['removed_gallery_files'],1);self.assertFalse(Path(returned['path']).exists())

    def test_later_restore_cancels_completed_delete_request(self):
        row=self.file('one.jpg');digest=row['sha256']
        (self.config['delete_folder']/'photo.jpg').write_bytes(b'photo')
        deletion.service(self.config,self.recycler)
        requests=deletion.intents(self.config)
        requests['items'][digest]['requested_utc']='2000-01-01T00:00:00Z'
        deletion.write_json(self.config['review_state'].parent/'delete-requests.json',requests)
        (self.config['restore_folder']/'photo.jpg').write_bytes(b'photo')
        self.restore()
        result=deletion.service(self.config,self.recycler)
        self.assertFalse(result['held'])
        self.assertEqual(deletion.intents(self.config)['items'][digest]['status'],'cancelled_by_restore')
        self.assertNotIn(digest,json.loads(self.config['deletion_ledger'].read_text())['folder_deleted_sha256'])

    def test_native_live_photo_review_is_held_until_complete_bundle_preserved(self):
        row=self.file('live.heic',library='icloud');row['native_live_photo']=True
        result=routes.route(self.config,[row],self.out,True,self.recycler)
        self.assertEqual(result['moved_files'],0);self.assertTrue(result['held']);self.assertTrue(Path(row['path']).exists())

    def test_combined_cleanup_intake_reuses_its_single_inventory(self):
        row=self.file('one.jpg')
        (self.config['delete_folder']/'photo.jpg').write_bytes(b'photo')
        with patch('photo_pass.inventory',side_effect=AssertionError('No extra gallery crawl')):
            result=deletion.service({**self.config,'defer_review_deletions':True},self.recycler)
        self.assertEqual(result['gallery_crawls'],0);self.assertEqual(result['pending_requests'],1)
        self.assertTrue(Path(row['path']).exists())
        done=deletion.execute(self.config,deletion.intents(self.config),[row],self.recycler)
        self.assertEqual(done['removed_gallery_files'],1)

    def test_verification_reports_returned_sources_and_changed_preservation(self):
        rows,result=self.moved()
        self.assertEqual(result['verification']['absent_sources'],2)
        Path(rows[0]['path']).write_bytes(b'photo')
        checked=routes.verify_moves(self.config,self.out)
        self.assertEqual(checked['absent_sources'],1)
        self.assertEqual(checked['returned_sources'],[rows[0]['path']])
        self.assertTrue(Path(rows[0]['path']).exists())
        (self.config['review_folder']/'Memes and cartoons'/'original.jpg').write_bytes(b'changed')
        self.assertEqual(routes.verify_moves(self.config,self.out)['verified_receipts'],0)
