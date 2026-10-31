import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import icloud_private_album as apple
import personal_vault_transfer as vault
import nsfw_review as inbox
import review_queue as queue
import photos
import test_nsfw_review as fixtures


class InboxTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.FolderDecisionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.config=fixture.config;self.copy=fixture.copy;self.digest=fixture.digest
        self.origin=self.config['roots']['onedrive']/'fixture.jpg'

    def restore_input(self):
        path=self.config['restore_folder']/'keep.jpg';self.copy.rename(path);return path

    def test_old_kept_leftover_is_reverified_restored_and_cleared(self):
        path=self.restore_input();state=queue.load_state(self.config['review_state'])
        state['items'][self.digest]['status']='kept';queue.save_state(self.config['review_state'],state)
        self.origin.unlink()
        with patch('sync_local_photo_folders.embedded_hold',return_value=None):result=inbox.service(self.config)
        self.assertFalse(result['held']);self.assertEqual(self.origin.read_bytes(),b'photo')
        self.assertFalse(path.exists());self.assertEqual(result['restore']['restore_files_cleared'],1)
        self.assertEqual(result['restore']['new_kept_hashes'],0)
        item=queue.load_state(self.config['review_state'])['items'][self.digest]
        self.assertEqual(Path(item['restore_backup']).read_bytes(),b'photo')

    def test_restore_cleanup_failure_is_visible_and_retryable(self):
        path=self.restore_input()
        with patch('privacy_storage.copy_verified',side_effect=OSError('backup inaccessible')):
            first=inbox.service(self.config)
        self.assertTrue(first['held']);self.assertTrue(path.exists())
        second=inbox.service(self.config)
        self.assertFalse(second['held']);self.assertFalse(path.exists())

    def test_hardlinked_backup_does_not_allow_restore_cleanup(self):
        import os
        path=self.restore_input();folder=self.config['review_state'].parent/'restore-preserved';folder.mkdir()
        os.link(path,folder/'fixture.jpg')
        result=inbox.service(self.config)
        self.assertTrue(result['held']);self.assertTrue(path.exists())

    def test_empty_inboxes_do_not_log_in_or_scan_cloud(self):
        with patch.object(apple,'run',side_effect=AssertionError('No cloud action')),patch.object(vault,'run',side_effect=AssertionError('No Vault action')):
            result=inbox.service(self.config)
        self.assertFalse(result['held']);self.assertEqual(result['delivery'],{})

    def test_changed_original_is_never_overwritten_or_cleared(self):
        path=self.restore_input();self.origin.write_bytes(b'other')
        result=inbox.service(self.config)
        self.assertTrue(result['held']);self.assertTrue(path.exists());self.assertEqual(self.origin.read_bytes(),b'other')

    def test_onedrive_only_cleanup_still_delivers_confirmed_to_apple(self):
        self.copy.rename(self.config['nsfw_confirmed_folder']/'approved.jpg')
        with patch.object(apple,'run',return_value={'held':[]}) as cloud,patch.object(vault,'run',return_value={'held':[]}) as transfer:
            result=inbox.service({**self.config,'active_galleries':['onedrive']})
        cloud.assert_called_once();transfer.assert_called_once()
        self.assertFalse(cloud.call_args.kwargs['interactive'])
        self.assertFalse(result['apple_actions_deferred'])

    def test_confirmed_attempts_both_destinations_with_cached_login_only(self):
        self.copy.rename(self.config['nsfw_confirmed_folder']/'approved.jpg')
        self.config['icloud_username']='fixture@example.com'
        with patch.object(apple,'run',return_value={'held':[]}) as cloud,patch.object(vault,'run',return_value={'held':[]}) as transfer:
            result=inbox.service(self.config)
        self.assertFalse(result['held']);cloud.assert_called_once()
        self.assertFalse(cloud.call_args.kwargs['interactive']);self.assertEqual(cloud.call_args.kwargs['digests'],[self.digest])
        transfer.assert_called_once()

    def test_previous_apple_receipt_never_skips_fresh_confirmed_processing(self):
        self.copy.rename(self.config['nsfw_confirmed_folder']/'approved.jpg');inbox.process(self.config,True)
        state=queue.load_state(self.config['review_state']);state['items'][self.digest]['apple_private_album']={'all_matches_verified':True,'verified_asset_ids':['asset']}
        queue.save_state(self.config['review_state'],state)
        with patch.object(apple,'run',return_value={'held':[]}) as cloud,patch.object(vault,'run',return_value={'held':[{'error':'Unlock Vault'}]}):
            result=inbox.service(self.config)
        cloud.assert_called_once();self.assertEqual(cloud.call_args.kwargs['digests'],[self.digest])
        self.assertEqual(result['held'][0]['provider'],'personal_vault')
        self.assertTrue(list(self.config['nsfw_confirmed_folder'].glob('*.jpg')))

    def test_provider_failure_does_not_skip_other_destination(self):
        self.copy.rename(self.config['nsfw_confirmed_folder']/'approved.jpg')
        with patch.object(apple,'run',side_effect=ValueError('expired')),patch.object(vault,'run',return_value={'held':[{'error':'Apple pending'}]}) as transfer:
            result=inbox.service(self.config)
        self.assertEqual(len(result['held']),2);transfer.assert_called_once()

    def test_launcher_services_folders_before_processing_even_for_plan(self):
        self.config['dependencies']=[];self.config['deletion_ledger']=Path('unused')
        result={'restore':{},'new_confirmations':0,'held':[]}
        events=[]
        with patch('sys.argv',['photos.py','pass']),patch.object(photos.settings,'load',return_value=self.config),patch.object(photos.settings,'decisions'), \
             patch('restore_family.before_run',side_effect=lambda *a:events.append('family')), \
             patch.object(inbox,'service',side_effect=lambda *a,**kw:events.append('inboxes') or result), \
             patch.object(photos.subprocess,'run',side_effect=lambda *a,**kw:events.append('task') or SimpleNamespace(returncode=0)):
            self.assertEqual(photos.main(),0)
        self.assertEqual(events,['family','inboxes','task'])

    def test_help_does_not_process_inboxes(self):
        self.config['dependencies']=[]
        with patch('sys.argv',['photos.py','pass','--help']),patch.object(photos.settings,'load',return_value=self.config), \
             patch.object(inbox,'service',side_effect=AssertionError('Help is read only')),patch.object(photos.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(photos.main(),0)

    def test_review_verification_does_not_process_inboxes(self):
        self.config['dependencies']=[]
        self.config['deletion_ledger']=Path('unused')
        with patch('sys.argv',['photos.py','review-pending','--run','fixture','--verify']),\
             patch.object(photos.settings,'load',return_value=self.config),patch.object(photos.settings,'decisions'),\
             patch.object(inbox,'service',side_effect=AssertionError('Verification is read only')),\
             patch.object(photos.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(photos.main(),0)


if __name__=='__main__':unittest.main()
