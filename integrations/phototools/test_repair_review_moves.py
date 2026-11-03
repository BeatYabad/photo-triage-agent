import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import repair_review_moves as repair


class RepairReviewTests(unittest.TestCase):
    def test_changed_bytes_cannot_reuse_old_nomination(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'photo.jpg';source.write_bytes(b'new')
            row={'path':str(source),'sha256':'a'*64,'bytes':3}
            (root/'files.jsonl').write_text(json.dumps(row)+'\n')
            (root/'workflow-summary.json').write_text(json.dumps({
                'scan':{'streaming_cleanup':{'held':[{'paths':[str(source)]}]}},
                'verification':{'holds':[]}}))
            with patch.object(repair.settings,'load',return_value={}), \
                 patch('restore_family.before_run'), \
                 patch.object(repair.queue,'fresh',return_value={'sha256':'b'*64,'bytes':3}), \
                 patch.object(repair.review_routes,'route',return_value={}) as route:
                repair.run(root,True)
            self.assertEqual(route.call_args.args[1],[])
            self.assertEqual(source.read_bytes(),b'new')
            report=json.loads((root/'review-repair'/'repair-summary.json').read_text())
            self.assertIn('differs',report['preparation_issues'][0]['error'])


if __name__=='__main__':unittest.main()
