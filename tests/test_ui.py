import unittest


class CardUITests(unittest.TestCase):
    def test_cards_search_paging_and_user_selected_pins(self):
        import asyncio
        import sys
        import tempfile
        import time
        from pathlib import Path
        from unittest.mock import patch

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QImage, QColor, QPainter, QFont
        from PySide6.QtTest import QTest
        from app import Window
        from core import Index, video_record
        from test_core import message

        app = QApplication.instance() or QApplication([])
        tmp = tempfile.TemporaryDirectory()
        data_dir = Path(tmp.name)
        fixtures = []
        for i, color in enumerate(['#315d83', '#397a79', '#836b8e']):
            fixture = data_dir / f'test-thumb-{i}.png'
            img = QImage(360 if i == 1 else 640, 640 if i == 1 else 360, QImage.Format.Format_RGB32)
            img.fill(QColor(color))
            painter = QPainter(img)
            painter.setPen(QColor('#f5f8fb'))
            painter.setFont(QFont('Microsoft YaHei UI', 24))
            painter.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, '预览图测试\n' + ('竖屏' if i == 1 else '横屏'))
            painter.end()
            img.save(str(fixture))
            fixtures.append(fixture)
        w = Window(data_dir)
        w.show()

        def pump_until(predicate, timeout=5):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                app.processEvents()
                if predicate():
                    return
                time.sleep(.01)
            raise AssertionError('UI timeout')

        requests = []
        try:
            pump_until(lambda: w.worker_ready)
            async def seed():
                service = w.worker.service
                service.index = Index(data_dir / 'test.sqlite3')
                service.index.channel(123, '测试频道', '')
                service.index.channel(456, 'A频道（模拟）', '')
                service.index.save_batch([video_record(message(i, f'关键词甲_第{i}期_关键词乙.mp4', '产品介绍 / 摄影讲解'), 123)
                                         for i in range(1, 14)] + [video_record(message(14, '关键词甲_其他内容.mp4'), 123)], 123, 14)
                async def thumbnail(channel_id, message_id):
                    requests.append((channel_id, message_id))
                    await asyncio.sleep(.03)
                    return {'channel_id': channel_id, 'message_id': message_id,
                            'status': 'missing' if message_id == 11 else 'ready',
                            'path': str(fixtures[message_id % 3])}
                service.thumbnail = thumbnail
                return service.index.channels()

            channels = asyncio.run_coroutine_threadsafe(seed(), w.worker.loop).result(timeout=3)
            w.authenticated = True
            w.account_label.setText('已登录：测试账号（模拟）')
            for widget in (w.connect_btn, w.qr_btn):
                widget.hide()
            w.logout_btn.show()
            w.fill_channels(channels)
            assert not w.pinned_channels and w.channel.itemData(0) is None
            w.channel.setCurrentIndex(w.channel.findData(123))
            w.pin_btn.click()
            assert w.channel.itemData(0) == 123 and w.channel.itemText(0).startswith('★')
            w.query.setText('关键词甲')
            w.second_query.setText('关键词乙')
            QTest.keyClick(w.second_query, Qt.Key.Key_Return)
            pump_until(lambda: not w.search_busy and len(w.preview_results) == 6 and not w.preview_jobs)
            assert len(w.rows) == 6 and w.total == 13 and w.page_size == 6
            assert len(requests) == 6
            assert not w.results.cards[0].preview.pixmap.isNull()
            assert w.results.cards[2].preview.placeholder == '此视频没有预览图'
            assert w.preview_btn.isEnabled() and not w.credentials.isVisible()
            assert not w.detail.isVisible()
            card = w.results.cards[0]
            six_card_width = card.width()
            assert card.preview.height() >= 170 and card.preview.height() > 3 * (card.title.height() + card.meta.height())
            with patch('app.desktop_executable', return_value='C:/Applications/Telegram Desktop/Telegram.exe'), patch('app.launch_desktop') as start:
                QTest.mouseDClick(w.results.cards[1], Qt.MouseButton.LeftButton)
                assert start.call_args.args[1] == 'tg://privatepost?channel=123&post=12'
            w.results.select(0)

            old_generation = w.preview_generation
            old_kind = f'thumbnail:{old_generation}:123:13'
            w.preview_jobs[old_kind] = (old_generation, (123, 13))
            w.next_btn.click()
            pump_until(lambda: not w.search_busy and w.page == 1)
            w.on_thumbnail(old_kind, {'status': 'ready', 'path': str(fixtures[0])}, '')
            assert (123, 13) not in w.row_by_key
            assert len(w.rows) == 6 and w.next_btn.isEnabled()
            w.next_btn.click()
            pump_until(lambda: not w.search_busy and w.page == 2)
            assert len(w.rows) == 1 and not w.next_btn.isEnabled()
            w.prev_btn.click()
            pump_until(lambda: not w.search_busy and w.page == 1)
            w.page_size_choice.setCurrentIndex(1)
            pump_until(lambda: not w.search_busy and w.page == 0 and len(w.preview_results) == 4 and not w.preview_jobs)
            assert len(w.rows) == 4 and w.page_size == 4 and w.results.columns == 2
            assert w.results.cards[0].width() > six_card_width
            w.detail_btn.click()
            assert w.detail.isVisible() and '关键词乙' in w.detail.toPlainText()
            w.detail_btn.click()
            app.processEvents()

            w.preview_toggle.setChecked(False)
            assert w.results.cards[0].preview.pixmap.isNull() and not w.preview_btn.isEnabled()
            w.preview_toggle.setChecked(True)
            pump_until(lambda: len(w.preview_results) == 4 and not w.preview_jobs)
            # Refresh preserves both the pinned order and selected channel.
            w.fill_channels(channels)
            assert w.channel.itemData(0) == 123 and w.channel.currentData() == 123
            w.pin_btn.click()
            assert not w.pinned_channels and w.channel.itemData(0) is None
            w.query.setText('找不到的关键词')
            w.search(0)
            pump_until(lambda: not w.search_busy)
            assert not w.results.cards and not w.next_btn.isEnabled() and not w.open_btn.isEnabled()
            print('Card UI PASS: 6/4 paging, separated keywords, pinned channel, landscape/portrait previews, click/open, stale callbacks, empty results, toggle.')
        finally:
            w.close()
            pump_until(lambda: w.closed)
            assert not w.worker.isRunning()
            tmp.cleanup()
