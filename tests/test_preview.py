import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import types, errors
from core import Service, Index, static_thumbnail, desktop_link
from desktop import desktop_executable, launch_desktop


class LinkTests(unittest.TestCase):
    def test_public_and_private_desktop_links(self):
        self.assertEqual(desktop_link(123, '', 456), 'tg://privatepost?channel=123&post=456')
        self.assertEqual(desktop_link(123, 'example_channel', 456), 'tg://resolve?domain=example_channel&post=456')
        with self.assertRaises(ValueError):
            desktop_link(0, '', 1)

    def test_saved_executable_preserves_path_casing(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / 'telegram.exe'
            executable.touch()
            (Path(tmp) / 'desktop.json').write_text(json.dumps({'executable':str(executable)}), encoding='utf-8')
            self.assertEqual(desktop_executable(tmp), str(executable))

    def test_direct_launch_uses_argument_list_and_explicit_directory(self):
        executable = str(Path('folder with spaces') / 'telegram.exe')
        url = desktop_link(123, '', 4)
        with patch('desktop.subprocess.Popen') as start:
            launch_desktop(executable, url)
        self.assertEqual(start.call_args.args[0], [executable, '--', url])
        self.assertEqual(start.call_args.kwargs['cwd'], str(Path(executable).parent))
        self.assertNotIn('shell', start.call_args.kwargs)

    def test_static_thumbnail_never_selects_video_or_empty(self):
        small = types.PhotoSize('s', 90, 50, 1000)
        large = types.PhotoSize('m', 320, 180, 10000)
        video = types.VideoSize('v', 1280, 720, 300000)
        doc = SimpleNamespace(thumbs=[video, small, types.PhotoSizeEmpty('e'), large])
        self.assertIs(static_thumbnail(doc), large)
        self.assertIsNone(static_thumbnail(SimpleNamespace(thumbs=[video])))


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Service(Path(self.tmp.name))
        self.s.account_id = 100
        self.s.index = Index(Path(self.tmp.name) / 'test.sqlite3')
        self.s.client = AsyncMock()
        self.thumb = types.PhotoSize('m', 320, 180, 10000)
        self.s.client.get_messages.return_value = SimpleNamespace(document=SimpleNamespace(thumbs=[self.thumb]))
        self.s.client.download_media.return_value = b'jpeg-test-data'

    async def asyncTearDown(self):
        self.s.index.close()
        self.tmp.cleanup()

    async def test_download_is_explicit_thumbnail_and_cache_is_reused(self):
        result = await self.s.thumbnail(123, 4)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(Path(result['path']).read_bytes(), b'jpeg-test-data')
        self.assertIs(self.s.client.download_media.call_args.kwargs['thumb'], self.thumb)
        self.assertIs(self.s.client.download_media.call_args.kwargs['file'], bytes)
        self.assertEqual(await self.s.thumbnail(123, 4), result)
        self.s.client.get_messages.assert_awaited_once()
        self.s.client.download_media.assert_awaited_once()

    async def test_missing_thumbnail_never_downloads_full_video(self):
        self.s.client.get_messages.return_value = SimpleNamespace(document=SimpleNamespace(thumbs=[]))
        self.assertEqual((await self.s.thumbnail(123, 4))['status'], 'missing')
        self.s.client.download_media.assert_not_awaited()
        self.s.client.get_messages.return_value = None
        self.assertEqual((await self.s.thumbnail(123, 5))['status'], 'missing')

    async def test_cache_is_account_scoped_and_expires(self):
        first = await self.s.thumbnail(123, 4)
        self.s.account_id = 200
        second = await self.s.thumbnail(123, 4)
        self.assertNotEqual(first['path'], second['path'])
        os.utime(second['path'], (time.time() - 90000, time.time() - 90000))
        await self.s.thumbnail(123, 4)
        self.assertEqual(self.s.client.download_media.await_count, 3)

    async def test_flood_wait_pauses_other_preview_requests(self):
        self.s.client.get_messages.side_effect = errors.FloodWaitError(None, capture=60)
        self.assertEqual((await self.s.thumbnail(123, 4))['status'], 'rate_limited')
        self.assertEqual((await self.s.thumbnail(123, 5))['status'], 'rate_limited')
        self.s.client.get_messages.assert_awaited_once()

    async def test_cancellation_leaves_no_partial_cache(self):
        started = asyncio.Event()
        async def slow_download(*args, **kwargs):
            started.set()
            await asyncio.sleep(60)
        self.s.client.download_media.side_effect = slow_download
        task = asyncio.create_task(self.s.thumbnail(123, 4))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(list(Path(self.tmp.name).rglob('*.tmp')))
        self.assertFalse(list(Path(self.tmp.name).rglob('*.jpg')))

    async def test_new_search_cleans_previous_cache_but_paging_reuses_it(self):
        result = await self.s.thumbnail(123, 4)
        cached = Path(result['path'])
        await self.s.search(fresh=False, page=1)
        self.assertTrue(cached.exists())
        other_account = cached.parent.parent / '200'
        other_account.mkdir()
        other_file = other_account / '123_4.jpg'
        other_file.write_bytes(b'other account')
        note = cached.parent / 'keep.txt'
        note.write_text('not a generated preview')
        await self.s.search(fresh=True)
        self.assertFalse(cached.exists())
        self.assertTrue(other_file.exists())
        self.assertTrue(note.exists())

    async def test_new_search_waits_for_cancelled_download_before_cleanup(self):
        entered = asyncio.Event()
        completed_cleanup = asyncio.Event()
        async def slow_download(*args, **kwargs):
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                completed_cleanup.set()
        self.s.client.download_media.side_effect = slow_download
        download = asyncio.create_task(self.s.thumbnail(123, 4))
        await entered.wait()
        await self.s.search(fresh=True)
        self.assertTrue(completed_cleanup.is_set())
        self.assertTrue(download.cancelled())
        self.assertFalse(list(Path(self.tmp.name).rglob('*.jpg')))


if __name__ == '__main__':
    unittest.main()
