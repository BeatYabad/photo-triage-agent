import tempfile
import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
import two_library_duplicates as d
import review_queue as q

H='a'*64
def row(label,path,h=H):
    return {'library':label,'root':str(Path(path).parent),'path':path,'kind':'photo','status':'verified_local_bytes','sha256':h,'bytes':10,'mtime_ns':1}

class DuplicatePlanTests(unittest.TestCase):
    def test_untouched_keeper_sibling_does_not_block_standalone_extra(self):
        keeper={**row('icloud','/ic/a.mov'),'kind':'video'}
        extra={**row('icloud','/ic/b.mov'),'kind':'video'}
        sibling=row('icloud','/ic/a.jpg','b'*64)
        plan=d.build_plan([keeper,extra,sibling],set(),set(),set())
        self.assertEqual(len(plan['moves']),1)
        self.assertEqual(plan['moves'][0]['path'],extra['path'])
        self.assertIsNone(plan['moves'][0]['hold'])

    def test_live_photo_and_matching_still_are_distinct_not_held_duplicates(self):
        for marker in ({'native_live_photo':True},{'content_identifier':'PAIR'},
                       {'metadata':{'content_identifier':'PAIR'}}):
            with self.subTest(marker=marker):
                live={**row('icloud','/ic/live.jpg'),**marker}
                p=d.build_plan([live,row('icloud','/ic/still.jpg')],set(),set(),set())
                self.assertEqual(p['moves'],[])
                self.assertEqual(p['counts']['within_gallery_exact_extras'],0)
                self.assertEqual(p['counts']['distinct_live_photo_content_groups'],1)
                self.assertFalse(any(h.get('hold')=='live_photo_requires_complete_asset_plan' for h in p['holds']))

    def test_two_standalone_copies_still_deduplicate_beside_live_photo(self):
        live={**row('icloud','/ic/a-live.jpg'),'native_live_photo':True}
        p=d.build_plan([live,row('icloud','/ic/b.jpg'),row('icloud','/ic/c.jpg')],set(),set(),set())
        self.assertEqual(len(p['moves']),1)
        self.assertEqual(p['counts']['within_gallery_exact_extras'],1)
        self.assertEqual(p['moves'][0]['keeper']['path'],'/ic/b.jpg')
        self.assertIsNone(p['moves'][0]['hold'])

    def test_required_cross_library_copies_never_removed(self):
        p=d.build_plan([row('onedrive','/od/a.jpg'),row('icloud','/ic/a.jpg')],set(),set(),set())
        self.assertEqual(p['moves'],[]);self.assertEqual(p['copies'],[])

    def test_one_extra_with_same_library_keeper_and_both_required_copies(self):
        p=d.build_plan([row('onedrive','/od/a.jpg'),row('onedrive','/od/b.jpg'),row('icloud','/ic/a.jpg')],set(),set(),set())
        self.assertEqual(len(p['moves']),1);self.assertEqual(p['moves'][0]['keeper']['library'],'onedrive')
        self.assertIsNone(p['moves'][0]['hold']);self.assertEqual(set(p['moves'][0]['both_keepers']),{'onedrive','icloud'})

    def test_local_extra_does_not_wait_for_other_gallery(self):
        p=d.build_plan([row('onedrive','/od/a.jpg'),row('onedrive','/od/b.jpg')],set(),set(),set())
        self.assertIsNone(p['moves'][0]['hold']);self.assertEqual(len(p['copies']),1)

    def test_filename_hash_never_proves_placeholder_identity(self):
        cloud={**row('icloud','/ic/a__sha256-'+H+'.jpg'),'status':'placeholder_unverified'};cloud.pop('sha256')
        p=d.build_plan([row('onedrive','/od/a.jpg'),cloud],set(),set(),set())
        self.assertEqual(p['copies'][0]['hold'],'unavailable_same_size_possible_match');self.assertEqual(p['counts']['unverified_files'],1)

    def test_safe_content_still_deduplicated_and_overrides_old_deletion(self):
        p=d.build_plan([row('onedrive','/od/a.jpg'),row('onedrive','/od/b.jpg'),row('icloud','/ic/a.jpg')],{H},{H},set())
        self.assertIsNone(p['moves'][0]['hold'])

    def test_videos_deduplicated_independently_within_each_gallery(self):
        rows=[{**row(label,'/'+label+'/'+name+'.mp4'),'kind':'video'}
              for label in ('onedrive','icloud') for name in ('a','b')]
        with patch.object(d,'asset_evidence',return_value={'verified':True}):
            p=d.build_plan(rows,set(),set(),set(),Path('client.db'))
        self.assertEqual(len(p['moves']),2)
        self.assertTrue(all(r['keeper']['library']==r['library'] and not r['hold'] for r in p['moves']))
        self.assertEqual(p['copies'],[])

    def test_distinct_edits_are_not_duplicates(self):
        p=d.build_plan([row('onedrive','/od/a.jpg'),row('onedrive','/od/edit.jpg','b'*64),row('icloud','/ic/a.jpg')],set(),set(),set())
        self.assertFalse(p['moves']);self.assertEqual(len(p['copies']),1)

    def test_icloud_asset_mapping_not_required(self):
        p=d.build_plan([row('onedrive','/od/a.jpg'),row('icloud','/ic/a.jpg'),row('icloud','/ic/b.jpg')],set(),set(),set())
        self.assertIsNone(p['moves'][0]['hold'])

    def test_cloud_worker_preserves_default_resident_guard(self):
        self.assertFalse(q.FingerprintWorker().allow_cloud)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.jpg';p.write_bytes(b'photo');st=p.stat()
            class CloudStat:
                st_size=st.st_size;st_mtime_ns=st.st_mtime_ns;st_file_attributes=0x400000
                def __getattr__(self,name):return getattr(st,name)
            original=Path.stat
            with patch.object(Path,'stat',lambda x,*a,**kw:CloudStat() if x==p else original(x,*a,**kw)),patch('cloud_photo_bytes.hydrate') as hydrate:
                with self.assertRaisesRegex(ValueError,'Nonresident'):q.fingerprint(p)
                hydrate.assert_not_called()
                self.assertEqual(q.fingerprint(p,allow_cloud=True)['bytes'],5)
                hydrate.assert_called_once_with(p)

class MoveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);base=Path(self.tmp.name).resolve()
        space=patch.object(d.media,'space');space.start();self.addCleanup(space.stop)
        self.roots={'onedrive':base/'personal','icloud':base/'cloud'}
        self.config={'roots':self.roots,'review_folder':base/'FOR_REVIEW','preservation':base/'preservation',
                     'safe_ledgers':[],'deletion_ledger':base/'deletions.json','icloud_database':None}
        self.out=base/'runs'
        for p in [*self.roots.values(),self.out,self.config['review_folder'],self.config['preservation']]:p.mkdir()
        self.rows=[]
        for label,name in [('onedrive','a.jpg'),('onedrive','b.jpg'),('icloud','a.jpg')]:
            path=self.roots[label]/name;path.write_bytes(b'complete original')
            self.rows.append({**row(label,str(path)),**q.fingerprint(path)})
        self.plan=d.build_plan(self.rows,set(),set(),set())
        class Reader:
            def read(self,p):return q.fingerprint(p)
        self.reader=Reader()

    def apply(self,**kwargs):
        with patch.object(d,'ensure_digikam_closed'),patch.object(d.settings,'decisions',return_value=(set(),set())):
            return d.apply_moves(self.plan,self.config,self.out,self.roots,self.reader,**kwargs)

    def test_move_preserves_both_keepers_and_exact_path_undo(self):
        self.assertEqual(self.apply(),{'moved_onedrive':1})
        self.assertTrue((self.roots['onedrive']/'a.jpg').exists());self.assertTrue((self.roots['icloud']/'a.jpg').exists())
        self.assertFalse((self.roots['onedrive']/'b.jpg').exists())
        state=json.loads((self.out/'duplicate-state.json').read_text());item=next(iter(state['items'].values()))
        self.assertEqual(item['status'],'reviewing');self.assertTrue(Path(item['backup']).exists())
        with patch.object(d,'ensure_digikam_closed'):self.assertEqual(d.undo(self.config,self.out,self.roots,self.reader),{'restored_onedrive':1})
        self.assertEqual((self.roots['onedrive']/'b.jpg').read_bytes(),b'complete original')
        self.assertFalse(Path(item['review_path']).exists());self.assertTrue(Path(item['backup']).exists())

    def test_changed_keeper_prevents_move(self):
        (self.roots['icloud']/'a.jpg').write_bytes(b'different keeper')
        with self.assertRaisesRegex(ValueError,'Content changed'):self.apply()
        self.assertTrue((self.roots['onedrive']/'b.jpg').exists())

    def test_recycle_extra_preserves_same_gallery_and_other_gallery(self):
        self.config['duplicate_action']='recycle'
        recycled=[]
        def recycler(source):
            self.assertTrue((self.roots['onedrive']/'a.jpg').exists())
            self.assertTrue((self.roots['icloud']/'a.jpg').exists())
            recycled.append(source);Path(source).unlink()
        self.assertEqual(self.apply(recycler=recycler),{'recycled_onedrive':1})
        self.assertEqual(len(recycled),1)
        self.assertEqual(list((self.config['review_folder']/'DUPES').iterdir()),[])
        with patch.object(d.settings,'decisions',return_value=(set(),set())):
            self.assertEqual(d.verify_batch(self.config,self.out,self.roots,self.reader)['unresolved'],[])
        with patch.object(d,'ensure_digikam_closed'):
            self.assertEqual(d.undo(self.config,self.out,self.roots,self.reader),{'restored_onedrive':1})
        self.assertTrue((self.roots['onedrive']/'b.jpg').exists())

    def test_recycle_safe_extra_with_only_same_gallery_keeper(self):
        self.config['duplicate_action']='recycle'
        self.plan=d.build_plan(self.rows[:2],{self.rows[0]['sha256']},set(),{self.rows[0]['sha256']})
        (self.roots['icloud']/'a.jpg').unlink()
        with patch.object(d,'ensure_digikam_closed'),patch.object(d.settings,'decisions',return_value=({self.rows[0]['sha256']},set())):
            self.assertEqual(d.apply_moves(self.plan,self.config,self.out,self.roots,self.reader,recycler=lambda p:Path(p).unlink()),{'recycled_onedrive':1})
        self.assertTrue((self.roots['onedrive']/'a.jpg').exists())

    def test_recycle_failure_keeps_intent_and_blocks_repeat(self):
        self.config['duplicate_action']='recycle'
        with self.assertRaisesRegex(OSError,'provider'):
            self.apply(recycler=lambda p:(_ for _ in ()).throw(OSError('provider failed')))
        self.assertTrue((self.roots['onedrive']/'b.jpg').exists())
        with self.assertRaisesRegex(ValueError,'Unresolved duplicate'):
            self.apply(recycler=lambda p:Path(p).unlink())

    def test_changed_extra_prevents_move(self):
        (self.roots['onedrive']/'b.jpg').write_bytes(b'different extra')
        with self.assertRaisesRegex(ValueError,'Content changed'):self.apply()

    def test_review_files_without_ledger_fail_closed(self):
        folder=self.config['review_folder']/'DUPES';folder.mkdir();(folder/'unknown.jpg').write_bytes(b'unknown')
        with self.assertRaisesRegex(ValueError,'without ledger'):self.apply()

    def test_occupied_restore_path_not_overwritten(self):
        self.apply();p=self.roots['onedrive']/'b.jpg';p.write_bytes(b'new photo')
        with patch.object(d,'ensure_digikam_closed'):
            with self.assertRaisesRegex(ValueError,'Occupied'):d.undo(self.config,self.out,self.roots,self.reader)
        self.assertEqual(p.read_bytes(),b'new photo')

    def pending_icloud_copy(self):
        p=self.roots['icloud']/'b.jpg';p.write_bytes(b'complete original')
        extra={**row('icloud',str(p)),**q.fingerprint(p)};self.rows.append(extra)
        dbp=self.out/'client.db';db=sqlite3.connect(dbp)
        db.execute('CREATE TABLE fingerprint_paths(path TEXT,fingerprint TEXT,asset_key TEXT)')
        db.execute('CREATE TABLE server_items(asset_id TEXT,master_id TEXT,filename TEXT,dataclass_type INT,orig_size INT,is_deleted INT,is_expunged INT,is_deleted_locally INT,is_shared INT,has_edits INT,alt_size INT,compl_size INT,full_size INT,full_vid_size INT,med_vid_size INT)')
        db.execute('INSERT INTO fingerprint_paths VALUES(?,?,?)',(str(self.roots['icloud']/'a.jpg'),'keeper','resOriginalRes'))
        db.execute('INSERT INTO server_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('keeper','master','a.jpg',1,p.stat().st_size,0,0,0,0,0,0,0,0,0,0));db.commit();db.close()
        ledger=self.out/'delivery.jsonl'
        receipt={'action':'queued_local_verified','destination':str(p),'target':'icloud','source_retained':True,
                 'sha256':extra['sha256'],'bytes':extra['bytes'],'destination_mtime_ns':extra['mtime_ns']}
        ledger.write_text(json.dumps(receipt)+'\n');self.config['delivery_ledger']=ledger;self.config['icloud_database']=dbp
        return extra

    def test_verified_ordinary_import_extra_is_copied_before_provider_recycle(self):
        self.pending_icloud_copy()
        self.plan=d.build_plan(self.rows,set(),set(),set(),self.config['icloud_database'],self.config['delivery_ledger'])
        recycled=[]
        def recycler(source):
            self.assertTrue((self.config['review_folder']/'DUPES'/'b (2).jpg').exists())
            self.assertTrue((self.roots['icloud']/'a.jpg').exists());recycled.append(source);Path(source).unlink()
        self.assertEqual(self.apply(recycler=recycler),{'moved_onedrive':1,'moved_icloud':1});self.assertEqual(len(recycled),1)
        self.assertTrue((self.roots['icloud']/'a.jpg').exists());self.assertFalse((self.roots['icloud']/'b.jpg').exists())

    def test_unmapped_exact_copy_can_retire_with_mapped_native_keeper(self):
        extra=self.pending_icloud_copy();self.config['delivery_ledger'].write_text('')
        evidence=d.asset_evidence(extra,self.rows[2],self.config['icloud_database'],self.config['delivery_ledger'])
        self.assertEqual(evidence['resources'][0]['resource_key'],'unmapped_exact_local_copy')

    def test_unmapped_native_filename_is_held_for_provider_mapping(self):
        extra=self.pending_icloud_copy();self.config['delivery_ledger'].write_text('')
        with closing(sqlite3.connect(self.config['icloud_database'])) as db:
            db.execute('INSERT INTO server_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('unmapped-native','other-master','b.jpg',1,extra['bytes'],0,0,0,0,0,0,0,0,0,0))
            db.commit()
        with self.assertRaisesRegex(ValueError,'native filename match'):
            d.asset_evidence(extra,self.rows[2],self.config['icloud_database'],self.config['delivery_ledger'])

    def test_native_apple_keeper_preferred_over_earlier_import_filename(self):
        extra=self.pending_icloud_copy()
        original=Path(extra['path']);earlier=original.with_name('000-import.jpg');original.rename(earlier)
        extra['path']=str(earlier)
        self.config['delivery_ledger'].write_text('')
        plan=d.build_plan(self.rows,set(),set(),set(),self.config['icloud_database'],self.config['delivery_ledger'])
        cloud=[r for r in plan['moves'] if r['library']=='icloud']
        self.assertEqual(len(cloud),1);self.assertEqual(cloud[0]['path'],str(earlier))
        self.assertEqual(cloud[0]['keeper']['path'],str(self.roots['icloud']/'a.jpg'))
        self.assertIsNone(cloud[0]['hold'])

    def test_unmapped_placeholder_alias_of_native_keeper_is_held(self):
        extra=self.pending_icloud_copy();self.config['delivery_ledger'].write_text('')
        real_stat=Path.stat
        def cloud_stat(path,*args,**kwargs):
            st=real_stat(path,*args,**kwargs)
            if str(path)==extra['path']:return SimpleNamespace(st_file_attributes=0x400,st_mtime_ns=st.st_mtime_ns,st_size=st.st_size,st_mode=st.st_mode)
            return st
        identity={'sync_root_file_id':1,'identity_sha256':'same'}
        with patch.object(Path,'stat',cloud_stat),patch('cloud_photo_bytes.placeholder_identity',return_value=identity):
            with self.assertRaisesRegex(ValueError,'share a provider identity'):
                d.asset_evidence(extra,self.rows[2],self.config['icloud_database'],self.config['delivery_ledger'])

    def test_distinct_placeholder_identities_allow_exact_local_extra(self):
        extra=self.pending_icloud_copy();self.config['delivery_ledger'].write_text('')
        real_stat=Path.stat
        def cloud_stat(path,*args,**kwargs):
            st=real_stat(path,*args,**kwargs)
            if str(path)==extra['path']:return SimpleNamespace(st_file_attributes=0x400,st_mtime_ns=st.st_mtime_ns,st_size=st.st_size,st_mode=st.st_mode)
            return st
        identities=[{'sync_root_file_id':1,'identity_sha256':value} for value in ('extra','keeper')]
        with patch.object(Path,'stat',cloud_stat),patch('cloud_photo_bytes.placeholder_identity',side_effect=identities):
            self.assertEqual(d.asset_evidence(extra,self.rows[2],self.config['icloud_database'],self.config['delivery_ledger'])['resources'][0]['provider_identities'],identities)

    def test_pending_provider_rows_are_not_retried_unless_requested(self):
        cloud=[{**row('icloud',str(self.roots['icloud']/f'{n}.jpg')),'placeholder':True,'status':'inventoried'} for n in range(6)]
        for r in cloud:r.pop('sha256')
        previous=[{**r,'status':'provider_blocked'} for r in cloud]
        with patch.object(d,'inventory',return_value=(cloud,[])),patch.object(self.reader,'read',side_effect=OSError('Provider unavailable')) as read,patch.object(d.time,'sleep'):
            pending,_=d.refresh(self.roots,self.out,None,previous,self.reader)
            read.assert_not_called();self.assertEqual(len(pending),6)
            retried,_=d.refresh(self.roots,self.out,None,previous,self.reader,retry_unavailable=True)
            self.assertEqual(read.call_count,3);self.assertEqual(sum(r['status']=='provider_blocked' for r in retried),3)

    def test_native_icloud_undo_requires_original_asset_recovery(self):
        self.apply();path=self.out/'duplicate-state.json';state=json.loads(path.read_text());item=next(iter(state['items'].values()))
        item.update(library='icloud',root=str(self.roots['icloud']),source=str(self.roots['icloud']/'native-extra.jpg'),asset_evidence={'mode':'native_recycle','resources':[{'asset_id':'original-asset'}]})
        d.save_duplicate_state(path,state)
        with patch.object(d,'ensure_digikam_closed'):
            self.assertEqual(d.undo(self.config,self.out,self.roots,self.reader),{'icloud_recently_deleted_recovery_pending':1})
        self.assertFalse(Path(item['source']).exists());self.assertTrue(Path(item['review_path']).exists())

    def test_unresolved_move_intent_blocks_further_apply(self):
        self.apply();path=self.out/'duplicate-state.json';state=json.loads(path.read_text());next(iter(state['items'].values()))['status']='move_intent';d.save_duplicate_state(path,state)
        with self.assertRaisesRegex(ValueError,'Unresolved duplicate'):self.apply()

    def unsupported_rename_fixture(self):
        extra=self.pending_icloud_copy();backup=self.config['preservation']/'copy.heic';backup.write_bytes(Path(extra['path']).read_bytes())
        ident=d.move_identity(extra);item={'id':ident,'status':'move_intent','library':'icloud','source':extra['path'],'root':extra['root'],
              'sha256':extra['sha256'],'bytes':extra['bytes'],'backup':str(backup),'review_path':str(self.config['review_folder']/'DUPES'/'extra.jpg'),
              'keepers':{'onedrive':self.rows[0],'icloud':self.rows[2]},
              'asset_evidence':d.asset_evidence(extra,self.rows[2],self.config['icloud_database'],self.config['delivery_ledger'])}
        d.save_duplicate_state(self.out/'duplicate-state.json',{'version':1,'items':{ident:item}})
        return item

    def test_proven_unstarted_provider_rename_recovery(self):
        item=self.unsupported_rename_fixture();d.media.journal(self.out/'failure-history.jsonl',{'winerror':379,'source':item['source']})
        with patch.object(d,'ensure_digikam_closed'):
            self.assertEqual(d.recover_unsupported_rename(self.config,self.out,self.roots,self.reader),1)
        self.assertEqual(next(iter(json.loads((self.out/'duplicate-state.json').read_text())['items'].values()))['status'],'prepared')
        self.assertTrue(Path(item['source']).exists())

    def test_other_source_provider_error_cannot_authorize_recovery(self):
        item=self.unsupported_rename_fixture();d.media.journal(self.out/'failure-history.jsonl',{'winerror':379,'source':'other-source.jpg'})
        with patch.object(d,'ensure_digikam_closed'):
            with self.assertRaisesRegex(ValueError,'recorded unsupported'):d.recover_unsupported_rename(self.config,self.out,self.roots,self.reader)

    def test_scan_completion_receipt_does_not_depend_on_changing_summary(self):
        path=self.out/'files.jsonl';path.write_text(json.dumps(self.rows[0])+'\n')
        d.scan_receipt(path,[self.rows[0]]);(self.out/'summary.json').write_text('{"files":999}')
        self.assertEqual(len(d.read_scan(path)),1)
        path.write_text(json.dumps(self.rows[1])+'\n')
        with self.assertRaisesRegex(ValueError,'changed scan'):d.read_scan(path)

    def test_independent_batch_verification_detects_missing_keeper(self):
        self.apply()
        with patch.object(d.settings,'decisions',return_value=(set(),set())):
            result=d.verify_batch(self.config,self.out,self.roots,self.reader)
        self.assertEqual(result['moved_review_backup_and_both_keepers_full_hash_verified'],1)
        index=(self.out/'review-index.csv').read_text(encoding='utf-8-sig')
        self.assertIn(str(self.roots['icloud']/'a.jpg'),index.replace('\\\\','\\'))
        (self.roots['icloud']/'a.jpg').unlink()
        with self.assertRaises(FileNotFoundError):d.verify_batch(self.config,self.out,self.roots,self.reader)

if __name__=='__main__':unittest.main()
