import asyncio
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import plistlib
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


    def test_missing_established_state_preserves_both_copies(self):
        self.run_delivery([self.old])
        local, mirrored = self.local_files()[0], self.mirror_files()[0]
        before = local.read_bytes(), mirrored.read_bytes()
        (self.config['local_dir'] / '.state.json').unlink()
        for messages in ([], [self.new]):
            with self.assertRaisesRegex(ValueError, 'metadata.*missing.*restore'):
                self.run_delivery(messages)
            self.assertEqual((local.read_bytes(), mirrored.read_bytes()), before)
        self.assertEqual(self.new.downloads, 0)

    def test_missing_mirror_manifest_preserves_its_files(self):
        self.run_delivery([self.old])
        old = self.mirror_files()[0]
        before = old.read_bytes()
        (self.config['mirror_dir'] / '.mirror.json').unlink()
        with self.assertRaisesRegex(ValueError, 'metadata.*missing.*restore'):
            self.run_delivery([])
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse((self.config['mirror_dir'] / '.mirror.json').exists())

    def test_first_run_without_matches_initializes_empty_metadata(self):
        self.run_delivery([])
        self.assertEqual(pr.read_json(self.config['local_dir'] / '.state.json'), {'files': {}, 'latest': {}})
        self.assertEqual(pr.read_json(self.config['mirror_dir'] / '.mirror.json'), {})
        self.run_delivery([self.old])
        self.assertEqual(len(self.mirror_files()), 1)

    def test_partial_mirror_then_newer_issue_has_no_untracked_files(self):
        from unittest.mock import patch
        self.config['publications'] = demo_config(self.root)['publications'][:2]
        magazine = DemoMessage(10, 'Weekend-old.pdf')
        replacement_magazine = DemoMessage(11, 'Weekend-new.pdf')
        newest = DemoMessage(3, 'Morning-newest.pdf')
        self.run_delivery([magazine, self.old])
        original = pr.shutil.copyfile
        def fail_second(src, dst):
            if Path(src).parent.name == 'weekend-review':
                raise OSError('Synthetic failure after first copy')
            return original(src, dst)
        with patch('paperroute.shutil.copyfile', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.run_delivery([replacement_magazine, self.new])
        for _ in range(2):
            self.run_delivery([replacement_magazine, newest])
            manifest = pr.read_json(self.config['mirror_dir'] / '.mirror.json')
            actual = {str(p.relative_to(self.config['mirror_dir'])) for p in self.mirror_files()}
            self.assertEqual(actual, set(manifest))
            self.assertEqual(len(actual), 2)
            self.assertFalse(any(name.endswith('Morning-new.pdf') for name in actual))

    def test_mirror_ownership_survives_interruption_at_promotion(self):
        from unittest.mock import patch
        self.run_delivery([self.old])
        original = pr.os.replace
        for after_promotion in (False, True):
            message = DemoMessage(20 + int(after_promotion), 'Morning-intermediate.pdf')
            def interrupt(src, dst):
                if self.config['mirror_dir'] in Path(dst).parents and Path(dst).suffix == '.pdf':
                    manifest = pr.read_json(self.config['mirror_dir'] / '.mirror.json')
                    name = str(Path(dst).relative_to(self.config['mirror_dir']))
                    self.assertIn(name, manifest)
                    if after_promotion:
                        original(src, dst)
                    raise OSError('Synthetic promotion interruption')
                return original(src, dst)
            with patch('paperroute.os.replace', side_effect=interrupt):
                with self.assertRaises(OSError):
                    self.run_delivery([message])
            self.run_delivery([DemoMessage(30 + int(after_promotion), 'Morning-latest.pdf')])
            manifest = pr.read_json(self.config['mirror_dir'] / '.mirror.json')
            self.assertEqual(len(self.mirror_files()), 1)
            self.assertEqual({str(p.relative_to(self.config['mirror_dir'])) for p in self.mirror_files()}, set(manifest))

    def test_manifest_write_failure_cannot_promote_untracked_pdf(self):
        from unittest.mock import patch
        self.run_delivery([self.old])
        old = self.mirror_files()[0]
        original = pr.write_json
        def fail_manifest(path, data):
            if Path(path).name == '.mirror.json':
                raise OSError('Synthetic manifest failure')
            return original(path, data)
        with patch('paperroute.write_json', side_effect=fail_manifest):
            with self.assertRaises(OSError):
                self.run_delivery([self.new])
        self.assertEqual(self.mirror_files(), [old])
        self.run_delivery([DemoMessage(3, 'Morning-newest.pdf')])
        self.assertEqual(len(self.mirror_files()), 1)

    def test_edited_file_error_explains_recovery(self):
        self.run_delivery([self.old])
        self.local_files()[0].write_bytes(b'Personal annotations')
        stderr = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            with self.assertRaises(RuntimeError):
                asyncio.run(pr.run_delivery(DemoClient([self.old]), self.config))
        message = stderr.getvalue().lower()
        for phrase in ('morning-paper', 'edited', 'preserved', 'move', 'outside', 'retry'):
            self.assertIn(phrase, message)

    def test_untracked_collision_error_explains_recovery(self):
        self.run_delivery([self.old])
        state = pr.load_state(self.config['local_dir'])
        state['files'].clear()
        state['latest'].clear()
        pr.write_json(self.config['local_dir'] / '.state.json', state)
        stderr = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            with self.assertRaises(RuntimeError):
                asyncio.run(pr.run_delivery(DemoClient([self.old]), self.config))
        for phrase in ('morning-paper', 'untracked', 'preserved', 'move', 'outside', 'retry'):
            self.assertIn(phrase, stderr.getvalue().lower())

    def test_backend_error_message_stays_private(self):
        from unittest.mock import patch
        stderr = StringIO()
        with patch.object(DemoClient, 'get_entity', side_effect=RuntimeError('PRIVATE-ACCOUNT-DETAIL')):
            with redirect_stdout(StringIO()), redirect_stderr(stderr):
                with self.assertRaises(RuntimeError):
                    asyncio.run(pr.run_delivery(DemoClient([self.old]), self.config))
        self.assertNotIn('PRIVATE-ACCOUNT-DETAIL', stderr.getvalue())


    def test_retained_local_edit_has_complete_recovery_instructions(self):
        self.config['cleanup_confirmed'] = False
        self.run_delivery([self.old])
        self.run_delivery([self.new])
        old = next(p for p in self.local_files() if p.name.endswith('Morning-old.pdf'))
        mirror_old = self.config['mirror_dir'] / old.relative_to(self.config['local_dir'])
        original = old.read_bytes()
        before = (self.config['local_dir'] / '.state.json').read_bytes()
        old.write_bytes(b'Personal annotations on an older issue')
        with self.assertRaisesRegex(pr.RecoveryError, 'morning-paper.*edited.*preserved.*restore.*mirror or backup'):
            self.run_delivery([self.new])
        self.assertEqual(old.read_bytes(), b'Personal annotations on an older issue')
        saved = self.root / 'annotated-copy.pdf'
        old.replace(saved)
        with self.assertRaisesRegex(pr.RecoveryError, 'morning-paper.*missing.*restore.*mirror or backup'):
            self.run_delivery([self.new])
        self.assertEqual(mirror_old.read_bytes(), original)
        self.assertEqual((self.config['local_dir'] / '.state.json').read_bytes(), before)
        old.write_bytes(mirror_old.read_bytes())
        self.run_delivery([self.new])
        self.assertEqual(len(self.local_files()), 2)
        self.assertEqual(len(self.mirror_files()), 2)
        self.assertEqual(saved.read_bytes(), b'Personal annotations on an older issue')

    def test_missing_retained_local_pdf_preserves_mirror_on_no_match(self):
        self.config['cleanup_confirmed'] = False
        self.run_delivery([self.old])
        self.run_delivery([self.new])
        old = next(p for p in self.local_files() if p.name.endswith('Morning-old.pdf'))
        before = {p.name: p.read_bytes() for p in self.mirror_files()}
        old.unlink()
        with self.assertRaisesRegex(pr.RecoveryError, 'morning-paper.*missing.*restore.*mirror or backup'):
            self.run_delivery([])
        self.assertEqual({p.name: p.read_bytes() for p in self.mirror_files()}, before)
        self.assertEqual(len(pr.load_state(self.config['local_dir'])['files']), 2)


    def test_identical_untracked_mirror_file_is_never_adopted(self):
        destination = self.config['mirror_dir']
        self.config['mirror_dir'] = None
        self.run_delivery([self.old])
        identity = pr.claim_root(self.config['local_dir'])
        pr.claim_root(destination, identity)
        local = self.local_files()[0]
        untracked = destination / local.relative_to(self.config['local_dir'])
        untracked.parent.mkdir()
        untracked.write_bytes(local.read_bytes())
        original = untracked.read_bytes()
        self.config['mirror_dir'] = destination
        for messages in ([self.old], []):
            with self.assertRaisesRegex(pr.RecoveryError, 'untracked.*preserved'):
                self.run_delivery(messages)
            self.assertEqual(pr.read_json(destination / '.mirror.json'), {})
            self.assertEqual(untracked.read_bytes(), original)
        self.run_delivery([self.new])
        self.assertEqual(untracked.read_bytes(), original)
        self.assertNotIn(str(untracked.relative_to(destination)), pr.read_json(destination / '.mirror.json'))

    def test_concurrent_first_local_run_cannot_reinitialize_owner(self):
        entered, resume = threading.Event(), threading.Event()
        errors = []
        write_json = pr.write_json
        def pause_initialization(path, data):
            if threading.current_thread().name == 'local-owner' and Path(path).name == '.state.json' and not entered.is_set():
                entered.set()
                if not resume.wait(5):
                    raise RuntimeError('Test initialization timed out')
            write_json(path, data)
        def owner():
            try:
                asyncio.run(pr.run_delivery(DemoClient([self.old]), self.config))
            except Exception as exc:
                errors.append(exc)
        with patch.object(pr, 'write_json', side_effect=pause_initialization), redirect_stdout(StringIO()):
            thread = threading.Thread(target=owner, name='local-owner')
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(RuntimeError, 'Another PaperRoute run'):
                    asyncio.run(pr.run_delivery(DemoClient([]), self.config))
                self.assertFalse((self.config['local_dir'] / '.state.json').exists())
                self.assertFalse((self.config['local_dir'] / '.paperroute').exists())
            finally:
                resume.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(pr.load_state(self.config['local_dir'])['files']), 1)
        self.assertEqual((self.config['local_dir'] / '.paperroute').read_bytes(),
                         (self.config['mirror_dir'] / '.paperroute').read_bytes())
        self.run_delivery([self.old])
        self.assertEqual(self.old.downloads, 1)

    def test_concurrent_first_mirror_claim_cannot_replace_owner(self):
        destination = self.config['mirror_dir']
        self.config['mirror_dir'] = None
        self.run_delivery([self.old])
        self.config['mirror_dir'] = destination
        identity = pr.claim_root(self.config['local_dir'])
        other = demo_config(self.root / 'other')
        other_identity = pr.claim_root(other['local_dir'])
        other['mirror_dir'] = destination
        entered, resume = threading.Event(), threading.Event()
        errors = []
        write_json = pr.write_json
        def pause_initialization(path, data):
            if threading.current_thread().name == 'mirror-owner' and Path(path).name == '.mirror.json' and not entered.is_set():
                entered.set()
                if not resume.wait(5):
                    raise RuntimeError('Test initialization timed out')
            write_json(path, data)
        def owner():
            try:
                pr.mirror(self.config, pr.load_state(self.config['local_dir']), identity)
            except Exception as exc:
                errors.append(exc)
        with patch.object(pr, 'write_json', side_effect=pause_initialization), redirect_stdout(StringIO()):
            thread = threading.Thread(target=owner, name='mirror-owner')
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(RuntimeError, 'Another PaperRoute run'):
                    pr.mirror(other, pr.load_state(other['local_dir']), other_identity)
                self.assertFalse((destination / '.mirror.json').exists())
                self.assertFalse((destination / '.paperroute').exists())
            finally:
                resume.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual((destination / '.paperroute').read_text().strip(), identity)
        self.assertEqual(pr.read_json(destination / '.mirror.json'), pr.load_state(self.config['local_dir'])['files'])
        with self.assertRaisesRegex(ValueError, 'belongs to another'):
            pr.mirror(other, pr.load_state(other['local_dir']), other_identity)


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve()
        self.directory.chmod(0o700)
        self.args = SimpleNamespace(config=self.directory / 'config.json', command='run')
        pr.write_json(self.directory / 'credentials.json', {'api_id': 123, 'api_hash': '0' * 32})
        self.client = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                                      start=AsyncMock(), disconnect=AsyncMock())
        self.api_error = type('ApiIdInvalidError', (Exception,), {})
        modules = {'telethon': SimpleNamespace(TelegramClient=lambda *a, **kw: self.client),
                   'telethon.errors': SimpleNamespace(ApiIdInvalidError=self.api_error)}
        mocked = patch.dict(pr.sys.modules, modules)
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_network_deadline_covers_startup_and_commands(self):
        timeout = asyncio.timeout
        async def hang(*args, **kwargs):
            await asyncio.Future()
        async def dialogs():
            await hang()
            yield
        for phase in ('connect', 'is_user_authorized', 'channels', 'delivery'):
            with self.subTest(phase=phase):
                self.client.connect = AsyncMock(side_effect=hang if phase == 'connect' else None)
                self.client.is_user_authorized = AsyncMock(return_value=True, side_effect=hang if phase == 'is_user_authorized' else None)
                self.client.disconnect.reset_mock()
                self.client.iter_dialogs = dialogs
                self.args.command = 'channels' if phase == 'channels' else 'run'
                with patch.object(pr.asyncio, 'timeout', side_effect=lambda seconds: timeout(0.01)) as deadline:
                    with patch.object(pr, 'load_config', return_value={}), patch.object(pr, 'run_delivery', side_effect=hang):
                        async def run():
                            await asyncio.wait_for(pr.telegram_command(self.args), 0.25)
                        with self.assertRaises(TimeoutError):
                            asyncio.run(run())
                    deadline.assert_called_once_with(600)
                self.client.disconnect.assert_awaited_once()

    def test_interactive_login_has_no_network_run_deadline(self):
        self.args.command = 'login'
        with patch.object(pr.asyncio, 'timeout') as deadline, redirect_stdout(StringIO()):
            asyncio.run(pr.telegram_command(self.args))
        deadline.assert_not_called()
        self.client.start.assert_awaited_once()
        self.client.disconnect.assert_awaited_once()

    def test_rejected_api_credentials_explain_and_allow_retry(self):
        self.args.command = 'login'
        session = self.directory / 'telegram.session'
        session.write_bytes(b'Synthetic session placeholder')
        original = (self.directory / 'credentials.json').read_bytes()
        self.client.start.side_effect = self.api_error('PRIVATE-ACCOUNT-DETAIL')
        with self.assertRaises(pr.RecoveryError) as caught:
            asyncio.run(pr.telegram_command(self.args))
        message = str(caught.exception)
        for phrase in ('API', 'credentials.json', 'remove only', 'login'):
            self.assertIn(phrase, message)
        self.assertNotIn('PRIVATE-ACCOUNT-DETAIL', message)
        self.assertEqual((self.directory / 'credentials.json').read_bytes(), original)
        self.assertEqual(session.read_bytes(), b'Synthetic session placeholder')
        self.client.disconnect.assert_awaited_once()
        (self.directory / 'credentials.json').unlink()
        self.client.start.side_effect = None
        with patch.object(pr.getpass, 'getpass', side_effect=['456', '1' * 32]), redirect_stdout(StringIO()):
            asyncio.run(pr.telegram_command(self.args))
        self.assertEqual(pr.read_json(self.directory / 'credentials.json'), {'api_id': 456, 'api_hash': '1' * 32})
        self.assertEqual(session.read_bytes(), b'Synthetic session placeholder')


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
