import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon import types
from core import Index, Service, message_link, normalize, parse_proxy, video_record


def message(mid, filename="摄影入门.MP4", caption="光线 教程", mime="video/mp4"):
    attrs = [types.DocumentAttributeFilename(filename)]
    return SimpleNamespace(id=mid, message=caption, date=datetime(2026, 9, 1, tzinfo=timezone.utc),
                           document=SimpleNamespace(attributes=attrs, mime_type=mime, size=1024))


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index = Index(Path(self.tmp.name) / "index.sqlite3")
        self.index.channel(123, "测试频道", "test_channel")
        self.index.channel(456, "私有频道", None)
        records = [video_record(message(1), 123),
                   video_record(message(2, "ＰＹＴＨＯＮ_基础.mkv", "编程 100%"), 123),
                   video_record(message(3, "其他.mp4", "摄影 进阶"), 456)]
        self.index.save_batch(records, 123, 2)

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def test_chinese_substring_filename_and_caption(self):
        self.assertEqual(self.index.search("摄影")['total'], 2)
        self.assertEqual(self.index.search("摄影", field="filename")['total'], 1)
        self.assertEqual(self.index.search("光线", field="caption")['total'], 1)

    def test_unicode_case_and_multiple_terms(self):
        self.assertEqual(self.index.search("python 基础")['total'], 1)
        self.assertEqual(self.index.search("摄影 编程", mode="any")['total'], 3)
        self.assertEqual(self.index.search("摄影 编程", mode="all")['total'], 0)

    def test_literal_special_characters(self):
        self.assertEqual(self.index.search("%")['total'], 1)
        self.assertEqual(self.index.search("_")['total'], 1)
        self.assertEqual(self.index.search("' OR 1=1 --")['total'], 0)

    def test_two_keywords_match_separated_positions_and_fields(self):
        self.index.save_batch([
            video_record(message(10, "关键词甲_新品讲解_关键词乙.mp4", ""), 123),
            video_record(message(11, "关键词乙_分享_关键词甲.mp4", ""), 123),
            video_record(message(12, "关键词甲介绍.mp4", "说明：关键词乙"), 123),
            video_record(message(13, "关键词甲介绍.mp4", "其他内容"), 123),
        ], 123, 13)
        result = self.index.search("关键词甲", second_query="关键词乙")
        self.assertEqual({r['message_id'] for r in result['rows']}, {10, 11, 12})
        self.assertEqual(self.index.search("关键词乙", second_query="关键词甲")['total'], 3)
        self.assertEqual(self.index.search("关键词甲", second_query="关键词乙", field="filename")['total'], 2)
        self.assertEqual(self.index.search("关键词甲", second_query="关键词乙", mode="any")['total'], 4)
        self.assertEqual(self.index.search("", second_query="关键词乙")['total'], 3)

    def test_scope_and_pagination(self):
        result = self.index.search("", channel_id=456)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['rows'][0]['link'], "https://t.me/c/456/3")
        first = self.index.search(page=0, page_size=2)
        second = self.index.search(page=1, page_size=2)
        self.assertEqual(first['total'], 3)
        self.assertEqual(len(second['rows']), 1)
        self.assertFalse(set(r['message_id'] for r in first['rows']) & set(r['message_id'] for r in second['rows']))

    def test_idempotence_and_monotonic_cursor(self):
        self.index.save_batch([video_record(message(1, caption="更新"), 123)], 123, 1)
        self.assertEqual(self.index.search()['total'], 3)
        self.assertEqual(self.index.search("更新")['total'], 1)
        self.assertEqual(self.index.cursor(123), 2)

    def test_video_file_detection(self):
        self.assertIsNotNone(video_record(message(7, "视频.MKV", mime="application/octet-stream"), 123))
        self.assertIsNone(video_record(message(7, "文字.pdf", mime="application/pdf"), 123))
        self.assertIsNone(video_record(SimpleNamespace(document=None), 123))

    def test_rebuild_commits_atomically(self):
        self.index.begin_rebuild()
        self.index.stage([video_record(message(4, "新.mp4"), 123)])
        self.assertEqual(self.index.search(channel_id=123)['total'], 2)
        self.index.finish_rebuild(123, 4)
        self.assertEqual(self.index.search(channel_id=123)['total'], 1)
        self.assertEqual(self.index.search(channel_id=456)['total'], 1)
        self.assertEqual(self.index.cursor(123), 4)

    def test_limits_and_proxy(self):
        with self.assertRaises(ValueError):
            self.index.search("x " * 13)
        self.assertEqual(parse_proxy("socks5://name:p%40ss@localhost:1080")['password'], "p@ss")
        self.assertIsNone(parse_proxy(""))
        with self.assertRaises(ValueError):
            parse_proxy("localhost:1080")
        self.assertEqual(message_link(123, "chan", 7), "https://t.me/chan/7")


class FakeClient:
    def __init__(self, messages, fail_after=None):
        self.messages = messages
        self.fail_after = fail_after
        self.options = None

    async def iter_messages(self, entity, **kwargs):
        self.options = kwargs
        count = 0
        for msg in sorted(self.messages, key=lambda m: m.id):
            if msg.id <= kwargs['min_id']:
                continue
            if self.fail_after is not None and count == self.fail_after:
                raise asyncio.CancelledError()
            count += 1
            yield msg
            await asyncio.sleep(0)


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(Path(self.tmp.name))
        self.service.index = Index(Path(self.tmp.name) / "test.sqlite3")
        self.service.entities[123] = SimpleNamespace(id=123, title="频道", username=None)

    async def asyncTearDown(self):
        self.service.index.close()
        self.tmp.cleanup()

    async def test_cancel_and_resume_without_missing_messages(self):
        msgs = [message(i) for i in range(1, 252)]
        self.service.client = FakeClient(msgs, fail_after=137)
        with self.assertRaises(asyncio.CancelledError):
            await self.service.sync(123)
        self.assertEqual(self.service.index.cursor(123), 137)
        self.assertEqual(self.service.index.search()['total'], 137)
        self.service.client = FakeClient(msgs)
        result = await self.service.sync(123)
        self.assertEqual(result['found'], 114)
        self.assertEqual(self.service.client.options['min_id'], 137)
        self.assertEqual(self.service.index.search()['total'], 251)

    async def test_rebuild_cancel_preserves_old_index(self):
        self.service.client = FakeClient([message(1), message(2)])
        await self.service.sync(123)
        self.service.client = FakeClient([message(1, caption="改名"), message(3)], fail_after=1)
        with self.assertRaises(asyncio.CancelledError):
            await self.service.sync(123, rebuild=True)
        self.assertEqual(self.service.index.search("改名")['total'], 0)
        self.assertEqual(self.service.index.search()['total'], 2)
        self.service.client = FakeClient([message(1, caption="改名"), message(3)])
        await self.service.sync(123, rebuild=True)
        self.assertEqual(self.service.index.search("改名")['total'], 1)
        self.assertEqual({r['message_id'] for r in self.service.index.search()['rows']}, {1, 3})

    async def test_non_video_messages_advance_cursor(self):
        self.service.client = FakeClient([SimpleNamespace(id=1, document=None), message(2)])
        result = await self.service.sync(123)
        self.assertEqual(result['seen'], 2)
        self.assertEqual(result['found'], 1)
        self.assertEqual(self.service.index.cursor(123), 2)


if __name__ == "__main__":
    unittest.main()
