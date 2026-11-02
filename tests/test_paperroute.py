import asyncio
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import plistlib
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import paperroute as pr
from demo import DemoClient, DemoMessage, demo_config


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.config = demo_config(self.root)
        self.config['publications'] = self.config['publications'][:1]
        self.old = DemoMessage(1, 'Morning-old.pdf')
        self.new = DemoMessage(2, 'Morning-new.pdf')

    def tearDown(self):
        self.tmp.cleanup()

    def run_delivery(self, messages):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            asyncio.run(pr.run_delivery(DemoClient(messages), self.config))

    def local_files(self):
        return sorted(self.config['local_dir'].glob('*/*.pdf'))

    def mirror_files(self):
        return sorted(self.config['mirror_dir'].glob('*/*.pdf'))

    def test_replacement_is_verified_then_old_is_removed(self):
        self.run_delivery([self.old])
        self.run_delivery([self.new, self.old])
        self.assertEqual(len(self.local_files()), 1)
        self.assertEqual(len(self.mirror_files()), 1)
        self.assertTrue(self.local_files()[0].name.endswith('Morning-new.pdf'))
        self.assertEqual(pr.digest(self.local_files()[0]), pr.digest(self.mirror_files()[0]))

    def test_failed_download_keeps_old_and_cleans_partial(self):
        self.run_delivery([self.old])
        self.new.fail = True
        with self.assertRaises(RuntimeError):
            self.run_delivery([self.new, self.old])
        self.assertTrue(self.local_files()[0].name.endswith('Morning-old.pdf'))
        self.assertTrue(self.mirror_files()[0].name.endswith('Morning-old.pdf'))
        self.assertFalse(list(self.config['local_dir'].rglob('*.part')))

    def test_non_pdf_and_incomplete_pdf_do_not_replace_old(self):
        self.run_delivery([self.old])
        for bad in (DemoMessage(2, 'Morning-new.pdf', b'not a PDF'), DemoMessage(3, 'Morning-short.pdf')):
            if bad.id == 3:
                bad.file.size += 10
            with self.assertRaises(RuntimeError):
                self.run_delivery([bad])
            self.assertTrue(self.local_files()[0].name.endswith('Morning-old.pdf'))

    def test_repeat_run_does_not_download_again(self):
        self.run_delivery([self.old])
        self.run_delivery([self.old])
        self.assertEqual(self.old.downloads, 1)

    def test_no_match_preserves_previous_issue(self):
        self.run_delivery([self.old])
        self.run_delivery([])
        self.assertEqual(len(self.local_files()), 1)
        self.assertEqual(len(self.mirror_files()), 1)

    def test_cleanup_requires_confirmation(self):
        self.config['cleanup_confirmed'] = False
        self.run_delivery([self.old])
        self.run_delivery([self.new])
        self.assertEqual(len(self.local_files()), 2)
        self.assertEqual(len(self.mirror_files()), 2)

    def test_unmanaged_files_survive(self):
        self.run_delivery([self.old])
        for root in (self.config['local_dir'], self.config['mirror_dir']):
            (root / 'morning-paper' / 'personal-notes.pdf').write_bytes(b'User-owned notes')
        self.run_delivery([self.new])
        self.assertEqual(len(self.local_files()), 2)
        self.assertEqual(len(self.mirror_files()), 2)

    def test_existing_nonempty_destination_is_not_adopted(self):
        self.config['mirror_dir'].mkdir()
        (self.config['mirror_dir'] / 'keep.txt').write_text('User file')
        with self.assertRaises(ValueError):
            self.run_delivery([self.old])
        self.assertEqual((self.config['mirror_dir'] / 'keep.txt').read_text(), 'User file')

    def test_edited_local_current_issue_is_not_overwritten(self):
        self.run_delivery([self.old])
        self.local_files()[0].write_bytes(b'Personal annotations')
        with self.assertRaises(RuntimeError):
            self.run_delivery([self.old])
        self.assertEqual(self.local_files()[0].read_bytes(), b'Personal annotations')

    def test_edited_mirror_previous_issue_is_not_deleted(self):
        self.run_delivery([self.old])
        old = self.mirror_files()[0]
        old.write_bytes(b'Personal annotations')
        with self.assertRaises(ValueError):
            self.run_delivery([self.new])
        self.assertEqual(old.read_bytes(), b'Personal annotations')

    def test_filename_traversal_stays_inside_publication(self):
        self.config['publications'][0]['patterns'] = ['*.pdf']
        self.run_delivery([DemoMessage(5, '../../escape.pdf')])
        self.assertEqual(len(self.local_files()), 1)
        self.assertEqual(self.local_files()[0].parent.name, 'morning-paper')
        self.assertFalse((self.root / 'escape.pdf').exists())

    def test_symlink_publication_is_rejected(self):
        pr.claim_root(self.config['local_dir'])
        outside = self.root / 'outside'
        outside.mkdir()
        (self.config['local_dir'] / 'morning-paper').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.run_delivery([self.old])
        self.assertEqual(list(outside.iterdir()), [])

    def test_corrupt_state_blocks_cleanup(self):
        self.run_delivery([self.old])
        (self.config['local_dir'] / '.state.json').write_text('{broken')
        with self.assertRaises(json.JSONDecodeError):
            self.run_delivery([self.new])
        self.assertTrue(self.local_files()[0].name.endswith('Morning-old.pdf'))

    def test_lock_blocks_second_run(self):
        pr.claim_root(self.config['local_dir'])
        with pr.lock_root(self.config['local_dir']):
            with self.assertRaises(RuntimeError):
                self.run_delivery([self.old])

    def test_failed_mirror_copy_does_not_delete_old(self):
        self.run_delivery([self.old])
        from unittest.mock import patch
        with patch('paperroute.shutil.copyfile', side_effect=OSError('Synthetic full disk')):
            with self.assertRaises(OSError):
                self.run_delivery([self.new])
        self.assertTrue(self.mirror_files()[0].name.endswith('Morning-old.pdf'))
        self.run_delivery([self.new])
        self.assertTrue(self.mirror_files()[0].name.endswith('Morning-new.pdf'))

    def test_interrupted_promotion_can_be_retried(self):
        self.run_delivery([self.old])
        from unittest.mock import patch
        original = pr.os.replace
        def fail_pdf(src, dst):
            if Path(dst).suffix == '.pdf':
                raise OSError('Synthetic interrupted rename')
            return original(src, dst)
        with patch('paperroute.os.replace', side_effect=fail_pdf):
            with self.assertRaises(RuntimeError):
                self.run_delivery([self.new])
        self.assertTrue(self.local_files()[0].name.endswith('Morning-old.pdf'))
        self.run_delivery([self.new])
        self.assertEqual(len(self.local_files()), 1)
        self.assertTrue(self.local_files()[0].name.endswith('Morning-new.pdf'))


class ConfigTests(unittest.TestCase):
    def test_schedule_uses_the_selected_config_and_interpreter_without_installing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = demo_config(root)
            config['local_dir'] = str(config['local_dir'])
            config['mirror_dir'] = None
            path = root / 'config.json'
            path.write_text(json.dumps(config))
            args = SimpleNamespace(config=path, hour=8, minute=15, install=False)
            with redirect_stdout(StringIO()):
                pr.schedule(args)
            data = plistlib.loads((root / 'com.paperroute.daily.plist').read_bytes())
            self.assertEqual(data['StartCalendarInterval'], {'Hour': 8, 'Minute': 15})
            self.assertEqual(data['ProgramArguments'][0], pr.sys.executable)
            self.assertEqual(data['ProgramArguments'][-2:], [str(path), 'run'])
            self.assertEqual(data['Umask'], 0o077)

    def test_paths_and_confirmation_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = demo_config(root)
            config['local_dir'] = str(config['local_dir'])
            config['mirror_dir'] = str(config['mirror_dir'])
            path = root / 'config.json'
            path.write_text(json.dumps(config))
            self.assertTrue(pr.load_config(path)['cleanup_confirmed'])
            for key, value in [('cleanup_confirmed', 'true'), ('mirror_dir', config['local_dir']), ('local_dir', './relative'), ('scan_limit', -1)]:
                changed = dict(config, **{key: value})
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    pr.load_config(path)


if __name__ == '__main__':
    unittest.main()
