import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from video_filter_review import aggregate


class VideoFilterReviewTests(unittest.TestCase):
    def frame(self,candidate=False,reasons=(),category=None,status='analyzed'):
        return dict(seconds=0,status=status,candidate=candidate,reasons=list(reasons),category=category)

    def test_dark_fade_does_not_nominate_useful_video(self):
        frames=[self.frame(True,['nearly_all_dark']),self.frame()]
        self.assertFalse(aggregate('quality',frames,2,10)['candidate'])

    def test_consistently_dark_video_goes_to_review(self):
        frames=[self.frame(True,['nearly_all_dark'])]*3
        result=aggregate('quality',frames,3,10)
        self.assertTrue(result['candidate'])
        self.assertEqual(result['reasons'],['nearly_all_dark'])
        self.assertIn('visual_review_required',result['holds'])

    def test_alternating_dark_bright_is_not_persistent_exposure(self):
        frames=[self.frame(True,['nearly_all_dark']),self.frame(True,['nearly_all_bright'])]
        self.assertFalse(aggregate('quality',frames,2,10)['candidate'])

    def test_supported_junk_retains_timestamp_and_category(self):
        frames=[self.frame(),self.frame(True,category='receipt')]
        frames[1]['seconds']=4
        result=aggregate('junk',frames,2,10)
        self.assertTrue(result['candidate'])
        self.assertEqual(result['category'],'receipt')
        self.assertEqual(result['frame_evidence'][1]['seconds'],4)

    def test_errors_or_missing_frames_are_holds(self):
        for frames in ([self.frame(True,category='receipt')],
                       [self.frame(True,category='receipt'),self.frame(status='error')]):
            result=aggregate('junk',frames,2,10)
            self.assertFalse(result['candidate'])
            self.assertEqual(result['status'],'analyzed_with_check_gaps')
            self.assertIn('check_incomplete',result['holds'])

    def test_generic_text_without_policy_support_is_not_junk(self):
        self.assertFalse(aggregate('junk',[self.frame()],1,1)['candidate'])

    def test_real_video_decodes_for_junk_and_quality(self):
        import cv2
        import numpy as np
        from photo_pass_analysis import Analyzer
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'synthetic.avi'
            writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),4,(64,48))
            self.assertTrue(writer.isOpened())
            try:
                for index in range(12):writer.write(np.full((48,64,3),128,np.uint8))
            finally:writer.release()
            analyzer=Analyzer({})
            with patch.object(analyzer,'ocr',return_value=([],(64,48))), patch.object(analyzer,'model',return_value=(None,None)), patch('triage_adapter.classify',return_value={'status':'analyzed','candidate':False}), patch('reference_ocr.classify',return_value=('analyzed',None,[])):
                results=analyzer.video(path,{'junk','quality'})
            for result in results.values():
                self.assertEqual(result['status'],'analyzed')
                self.assertFalse(result['candidate'])
                self.assertGreater(result['samples_read'],2)
            self.assertEqual(set(results),{'junk','quality'})

    def test_family_video_still_runs_nsfw(self):
        import cv2
        import numpy as np
        from photo_pass_analysis import Analyzer
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'family.avi'
            writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),4,(64,48))
            for _ in range(12):writer.write(np.full((48,64,3),128,np.uint8))
            writer.release()
            analyzer=Analyzer({})
            with patch('family_faces.load',return_value={'items':{'ref':{}}}),\
                 patch('family_faces.inspect_path',return_value={'matched':True}),\
                 patch.object(analyzer,'model',return_value=None),\
                 patch('triage_adapter.classify',side_effect=AssertionError('No junk for family')),\
                 patch('nsfw_analysis.frame_result',return_value={'candidate':False,'status':'analyzed'}) as nsfw:
                result=analyzer.video(path,{'junk','tiktok','nsfw'})
            self.assertFalse(result['junk']['candidate']);self.assertFalse(result['tiktok']['candidate'])
            self.assertGreater(nsfw.call_count,0)
            self.assertNotEqual(result['nsfw']['status'],'error')


if __name__=='__main__':unittest.main()
