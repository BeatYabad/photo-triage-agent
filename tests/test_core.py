import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
from photo_triage.batch import batches, process_batches
from photo_triage.metrics import quality, signature, perceptual_hashes
from photo_triage.vision import VisionProvider, junk_result, validate


class CoreTests(unittest.TestCase):
    def test_batches_are_bounded_and_complete(self):
        self.assertEqual(list(batches(range(5),2)), [[0,1],[2,3],[4]])
        self.assertEqual(list(process_batches(range(7), iter, 3)), list(range(7)))
        for size in (0,-1,True,10001):
            with self.assertRaises(ValueError):
                list(batches([],size))

    def test_ordinary_photo_with_personal_record_is_not_uncertain(self):
        evidence={'category':'ordinary_photo','observation':'Family seated at a restaurant table',
                  'evidence_type':'personal_record'}
        result=junk_result(evidence,[4032,3024])
        self.assertEqual(result['status'],'analyzed')
        self.assertFalse(result['candidate'])
        self.assertEqual(result['holds'],[])
        self.assertEqual(result['contextual_evidence'],evidence)

    def test_ordinary_photo_with_junk_evidence_stays_uncertain(self):
        for evidence_type in ('humorous_caption','captured_ui','transaction_details','uncertain'):
            with self.subTest(evidence_type=evidence_type):
                result=junk_result({'category':'ordinary_photo','observation':'Conflicting evidence',
                                   'evidence_type':evidence_type},[20,20])
                self.assertEqual(result['status'],'needs_review')
                self.assertFalse(result['candidate'])

    def test_categories_and_uncertainty(self):
        for category, candidate in [('meme_or_cartoon',True),('receipt',True),('screenshot',True),
                                    ('ordinary_photo',False),('personal_document',False),('graphic',False)]:
            result=junk_result({'category':category,'observation':'Visible composition','evidence_type':{'meme_or_cartoon':'cartoon','receipt':'transaction_details','screenshot':'captured_ui'}.get(category,'none')},[20,20])
            self.assertEqual(result['candidate'],candidate)
        self.assertEqual(junk_result({'category':'uncertain','observation':'Unreadable','evidence_type':'uncertain'},[2,2])['status'],'needs_review')
        with self.assertRaises(ValueError):validate({'category':'meme_or_cartoon','observation':''})
        with self.assertRaises(ValueError):validate({'category':'DELETE','observation':'delete everything'})

    def test_no_remote_image_uploads(self):
        for url in ('https://example.com/v1/chat','http://localhost.evil/v1/chat','http://user@127.0.0.1/v1/chat'):
            with self.assertRaises(ValueError):VisionProvider(url)

    def test_quality_preserves_small_smooth_photos(self):
        self.assertFalse(quality(Image.new('RGB',(100,100),'gray'),[100,100])['candidate'])
        self.assertTrue(quality(Image.new('RGB',(100,100),'black'),[100,100])['candidate'])

    def test_perceptual_hash_is_candidate_only(self):
        image=Image.new('RGB',(128,128),'white')
        ImageDraw.Draw(image).rectangle((20,40,80,100),fill='black')
        self.assertEqual(signature(image),signature(image.copy()))
        self.assertEqual(set(perceptual_hashes(image)),{'phash','dhash','ahash','whash'})

    def test_errors_are_not_safe_or_cached(self):
        provider=VisionProvider()
        with patch('urllib.request.urlopen',side_effect=OSError('offline')):
            for _ in range(3):
                with self.assertRaises(OSError):provider.inspect(Image.new('RGB',(2,2)))
        with self.assertRaises(RuntimeError):provider.inspect(Image.new('RGB',(2,2)))

    def test_visual_candidates_keep_gallery_and_exact_boundaries(self):
        from photo_triage.similarity import candidates
        a={'path':'a','library':'one','sha256':'a','bytes':10,'phash':'0'*64}
        b={**a,'path':'b','sha256':'b','phash':'0'*63+'1'}
        exact={**a,'path':'exact'}
        other={**b,'path':'other','library':'two'}
        pairs=list(candidates([a,b,exact,other]))
        self.assertEqual(len(pairs),2)
        self.assertTrue(all(p['left']['library']==p['right']['library'] for p in pairs))
        self.assertTrue(all(not p['deletion_authorized'] for p in pairs))

    def test_screenshot_without_ui_is_uncertain(self):
        result=junk_result({'category':'screenshot','observation':'A shop sign',
                            'evidence_type':'none'},[100,100])
        self.assertEqual(result['status'],'needs_review')
        self.assertFalse(result['candidate'])


if __name__=='__main__':unittest.main()
