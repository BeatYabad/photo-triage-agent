import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from photo_pass_analysis import Analyzer
import quality_analysis


class FilterPolicyTests(unittest.TestCase):
    def test_supported_meme_bypasses_nsfw_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fixture.png';Image.new('RGB',(100,100)).save(path)
            analyzer=Analyzer({})
            with patch.object(analyzer,'ocr',return_value=([],(100,100))), patch.object(analyzer,'model',return_value=(None,None)), patch('triage_adapter.classify',return_value={'status':'analyzed','candidate':True,'category':'meme_or_cartoon'}), patch('reference_ocr.classify',return_value=('analyzed',None,[])), patch('junk_policy.decide',side_effect=lambda result,*args:result), patch('nsfw_analysis.analyze_image',side_effect=AssertionError('Meme must bypass NSFW')) as nsfw:
                results=analyzer.image(path,{'junk','nsfw'})
            nsfw.assert_not_called()
            self.assertEqual(results['nsfw']['reason'],'supported_junk_before_nsfw')

    def test_junk_and_tiktok_skip_quality_and_nsfw(self):
        for junk, tiktok in ((True, False), (False, True)):
            with self.subTest(junk=junk,tiktok=tiktok), tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'fixture.png'
                Image.new('RGB',(100,100),(128,128,128)).save(path)
                analyzer=Analyzer({})
                with patch.object(analyzer,'ocr',return_value=([],(100,100))), patch.object(analyzer,'model',return_value=(None,None)), patch('triage_adapter.classify',return_value={'status':'analyzed','candidate':junk}), patch('reference_ocr.classify',return_value=('analyzed',None,[])), patch('junk_policy.decide',side_effect=lambda result,*args:result), patch('tiktok_video_triage.frame_features',return_value={'tiktok_text':tiktok}), patch('nsfw_analysis.analyze_image',return_value={'status':'analyzed','candidate':False}) as nsfw, patch('quality_analysis.analyze_image',side_effect=AssertionError('Junk must bypass quality')):
                    results=analyzer.image(path,{'quality','junk','nsfw','tiktok'})
                self.assertEqual(results['quality']['status'],'not_applicable')
                nsfw.assert_not_called()

    def test_old_small_smooth_photo_is_not_a_quality_candidate(self):
        result=quality_analysis.analyze_image(Image.new('RGB',(120,90),(128,128,128)),[120,90])
        self.assertFalse(result['candidate'])
        self.assertEqual(result['reasons'],[])


if __name__=='__main__':unittest.main()
