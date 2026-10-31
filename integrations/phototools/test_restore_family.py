import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from PIL import Image
import family_faces
import restore_family
import review_queue as queue
import review_routes
import two_library_duplicates as duplicates
from photo_pass_analysis import Analyzer

FACE=[1.]+[0.]*127


class RestoreFamilyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve()
        self.config={'roots':{k:self.base/k for k in ('onedrive','icloud','amazon')},
            'review_state':self.base/'state'/'review-state.json','restore_folder':self.base/'RESTORE',
            'review_folder':self.base/'FOR_REVIEW','nsfw_review_folder':self.base/'NSFW',
            'family_restore_folder':self.base/'RESTORE_FAMILY','output':self.base/'runs',
            'deletion_ledger':self.base/'deleted.json','safe_ledgers':[]}
        for root in [*self.config['roots'].values(),self.config['review_state'].parent,
                     self.config['family_restore_folder'],self.config['review_folder']]:root.mkdir(parents=True)
        self.config['deletion_ledger'].write_text('{"excluded_import_sha256":[]}')
        for name in ('a','b'):
            path=self.base/(name+'.json');path.write_text('{"excluded_sha256":[]}');self.config['safe_ledgers'].append(path)
        for p in (patch.object(queue,'fresh',side_effect=queue.fingerprint),
                  patch('windows_process_check.ensure_digikam_closed'),
                  patch('folder_ingest.ensure_digikam_closed')):
            p.start();self.addCleanup(p.stop)

    def input(self):
        source=self.config['family_restore_folder']/'family.png'
        Image.new('RGB',(80,80),'gray').save(source)
        identity=queue.fingerprint(source)
        original=self.config['roots']['icloud']/'family.png'
        origin={'path':str(original),'root':str(original.parent),'label':'icloud',**identity}
        queue.save_state(self.config['review_state'],{'version':1,'items':{identity['sha256']:{
            'status':'reviewing','origins':[origin],'representative':origin,
            'review_path':str(source),'evidence':[]}}})
        return source,original,identity

    def test_restore_enroll_clear_and_no_nsfw_exclusion(self):
        source,original,identity=self.input()
        with patch.object(restore_family,'encode_file',return_value=[FACE]):
            result=restore_family.process(self.config)
        self.assertEqual(result['restored'],1,result);self.assertFalse(result['held'])
        self.assertFalse(source.exists());self.assertEqual(queue.fingerprint(original)['sha256'],identity['sha256'])
        self.assertNotIn(identity['sha256'],queue.load_state(self.config['review_state'])['items'])
        for path in self.config['safe_ledgers']:self.assertEqual(json.loads(path.read_text())['excluded_sha256'],[])
        bank=family_faces.load(self.config);self.assertEqual(bank['items'][identity['sha256']]['faces'],[FACE])
        self.assertEqual(bank['items'][identity['sha256']]['restore_destinations'],[str(original)])

    def test_no_face_restores_exact_reference_and_reports_gap(self):
        source,original,identity=self.input()
        with patch.object(restore_family,'encode_file',return_value=[]):
            result=restore_family.process(self.config)
        self.assertEqual(result['restored'],1);self.assertEqual(len(result['enrollment_gaps']),1)
        self.assertTrue(family_faces.inspect(self.config,Image.new('RGB',(10,10)),identity)['matched'])

    def test_inference_failure_preserves_input_and_does_not_enroll(self):
        source,original,_=self.input()
        with patch.object(restore_family,'encode_file',side_effect=RuntimeError('Unavailable model')):
            result=restore_family.process(self.config)
        self.assertTrue(result['held']);self.assertTrue(source.exists());self.assertFalse(original.exists())
        self.assertFalse(family_faces.load(self.config)['items'])

    def test_restore_unknown_origin_uses_normal_fallback(self):
        source,original,identity=self.input()
        self.config['review_state'].unlink();self.config['review_state'].with_suffix('.created').unlink()
        fallback=self.config['roots']['onedrive']/'2026'/'10'/'family.png'
        def deliver(roots,src,item,stage,journal,index):
            fallback.parent.mkdir(parents=True);fallback.write_bytes(Path(src['path']).read_bytes());return fallback
        with patch.object(restore_family,'encode_file',return_value=[FACE]),\
             patch.object(queue,'restore_gallery_index',return_value=({},set())),\
             patch.object(queue,'deliver_restore_to_gallery',side_effect=deliver):
            result=restore_family.process(self.config)
        self.assertEqual(result['restored'],1);self.assertTrue(fallback.exists());self.assertFalse(original.exists())

    def test_family_protection_does_not_skip_nsfw_or_quality(self):
        analyzer=Analyzer({})
        with patch.object(family_faces,'inspect',return_value={'matched':True}),\
             patch.object(analyzer,'ocr',return_value=([],(100,100))),\
             patch.object(analyzer,'model',return_value=None),\
             patch('triage_adapter.classify',side_effect=AssertionError('Family must skip junk')),\
             patch('nsfw_analysis.analyze_image',return_value={'status':'analyzed','candidate':True,'category':'nsfw'}) as nsfw,\
             patch('quality_analysis.analyze_image',return_value={'status':'analyzed','candidate':False}) as quality:
            result=analyzer.decoded_image(Image.new('RGB',(100,100)),{'junk','tiktok','nsfw','quality'},[100,100])
        self.assertFalse(result['junk']['candidate']);self.assertFalse(result['tiktok']['candidate'])
        self.assertTrue(result['nsfw']['candidate']);nsfw.assert_called_once();quality.assert_called_once()

    def test_nsfw_only_never_consults_family_filter(self):
        analyzer=Analyzer({})
        with patch.object(family_faces,'inspect',side_effect=AssertionError('NSFW must not consult family')) as family,\
             patch.object(analyzer,'model',return_value=None),\
             patch('nsfw_analysis.analyze_image',return_value={'status':'analyzed','candidate':True}) as nsfw:
            result=analyzer.decoded_image(Image.new('RGB',(40,40)),{'nsfw'},[40,40])
        family.assert_not_called();nsfw.assert_called_once()
        self.assertTrue(result['nsfw']['candidate'])

    def test_failed_family_check_never_turns_into_a_junk_nomination(self):
        analyzer=Analyzer({})
        with patch.object(family_faces,'inspect',side_effect=ValueError('Bad face bank')),\
             patch('triage_adapter.classify',side_effect=AssertionError('No nomination on failed protection')):
            result=analyzer.decoded_image(Image.new('RGB',(40,40)),{'junk'},[40,40])
        self.assertEqual(result['junk']['status'],'error')

    def test_family_exact_duplicates_still_eligible(self):
        source,original,identity=self.input()
        rows=[{'path':str(original.with_name(n)),'library':'icloud','root':str(original.parent),
               'kind':'photo','status':'verified_local_bytes',**identity} for n in ('one.png','two.png')]
        plan=duplicates.build_plan(rows,set(),set(),set())
        self.assertEqual(plan['counts']['eligible_extra_files'],1)

    def test_stale_junk_candidate_is_protected_at_move_time(self):
        source,original,identity=self.input();original.write_bytes(source.read_bytes())
        row={'path':str(original),'root':str(original.parent),'library':'icloud','label':'icloud',
             'kind':'photo','status':'verified_local_bytes',**identity,
             'checks':{'junk':{'candidate':True,'category':'meme_or_cartoon','policy':'supported-junk-v1','confidence_band':'supported_review'}}}
        with patch.object(family_faces,'load',return_value={'items':{'ref':{}}}),\
             patch.object(family_faces,'inspect_path',return_value={'matched':True}):
            result=review_routes.route(self.config,[row],self.base/'routing',False)
        self.assertEqual(result.get('family_protected'),1);self.assertEqual(result['planned_files'],0)

    def test_reference_change_changes_rule_for_dependent_checks(self):
        before=family_faces.revision(self.config)
        family_faces.state_path(self.config).write_text(json.dumps({'version':1,'items':{'ref':{'faces':[FACE]}}}))
        self.assertNotEqual(before,family_faces.revision(self.config))

    def test_family_intake_failure_blocks_run_start(self):
        with patch.object(restore_family,'process',return_value={'held':[{'error':'unreadable input'}]}):
            with self.assertRaisesRegex(ValueError,'RESTORE_FAMILY'):
                restore_family.before_run(self.config)

    def test_direct_gallery_run_checks_family_before_health_or_import(self):
        import cleanup_one_gallery as cleanup
        with patch('sys.argv',['cleanup_one_gallery.py','--gallery','icloud','--output',str(self.base/'run'),'--apply']),\
             patch.object(cleanup.settings,'load',return_value=self.config),\
             patch.object(restore_family,'before_run',side_effect=ValueError('family first')) as family,\
             patch.object(cleanup,'check') as health,patch('scan_import.run') as imports:
            with self.assertRaisesRegex(ValueError,'family first'):cleanup.main()
        family.assert_called_once_with(self.config);health.assert_not_called();imports.assert_not_called()


if __name__=='__main__':unittest.main()

