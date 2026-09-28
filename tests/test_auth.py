import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from telethon import types, errors
from core import Service, code_delivery


class DeliveryTests(unittest.TestCase):
    def test_telegram_route_and_allowed_fallback(self):
        result = code_delivery(types.auth.SentCode(types.auth.SentCodeTypeApp(5), 'test-hash',
                                                   types.auth.CodeTypeSms(), 30))
        self.assertEqual(result['state'], 'code')
        self.assertIn('官方通知', result['delivery'])
        self.assertTrue(result['can_resend'])
        self.assertEqual(result['retry_after'], 30)

    def test_sms_email_and_missing_fallback(self):
        sms = code_delivery(types.auth.SentCode(types.auth.SentCodeTypeSms(5), 'test-hash'))
        self.assertIn('短信', sms['delivery'])
        self.assertFalse(sms['can_resend'])
        email = code_delivery(types.auth.SentCode(types.auth.SentCodeTypeEmailCode('a***@example.com', 6), 'test-hash'))
        self.assertIn('a***@example.com', email['delivery'])
        self.assertIn('邮箱', email['delivery'])

    def test_unsupported_route_does_not_claim_code_sent(self):
        result = code_delivery(types.auth.SentCode(types.auth.SentCodeTypeSetUpEmailRequired(), 'test-hash'))
        self.assertEqual(result['state'], 'unsupported')
        self.assertFalse(result['can_resend'])


class QR:
    url = 'tg://login?token=TEST_ONLY_NOT_VALID'

    def __init__(self, outcome='ok'):
        self.outcome = outcome
        self.started = asyncio.Event()
        self.accepted = asyncio.Event()
        self.waiting = False
        self.calls = 0
        self.recreate = AsyncMock()

    async def wait(self):
        self.waiting = True
        self.started.set()
        self.calls += 1
        try:
            await self.accepted.wait()
            if self.outcome == 'password':
                raise errors.SessionPasswordNeededError(None)
            if self.outcome == 'refresh' and self.calls == 1:
                raise asyncio.TimeoutError()
            return True
        finally:
            self.waiting = False


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Service(Path(self.tmp.name))
        self.s._open_client = AsyncMock()
        self.s._authorized = AsyncMock(return_value={'state': 'ready'})
        self.s.client = AsyncMock()
        self.s.client.is_user_authorized.return_value = False

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_phone_login_returns_actual_delivery(self):
        self.s.client.send_code_request.return_value = types.auth.SentCode(types.auth.SentCodeTypeSms(5), 'test-hash')
        result = await self.s.connect('1', '0' * 32, '+' + '0' * 11)
        self.assertIn('短信', result['delivery'])
        self.assertEqual(self.s.phone_code_hash, 'test-hash')

    async def test_resend_obeys_cooldown_and_updates_hash(self):
        self.s.phone = 'TEST_PHONE'
        self.s._code_response(types.auth.SentCode(types.auth.SentCodeTypeApp(5), 'old', types.auth.CodeTypeSms(), 30))
        with self.assertRaises(ValueError):
            await self.s.resend_code()
        self.s.client.assert_not_awaited()
        self.s.resend_at = time.monotonic() - 1
        self.s.client.return_value = types.auth.SentCode(types.auth.SentCodeTypeSms(5), 'new')
        result = await self.s.resend_code()
        self.assertIn('短信', result['delivery'])
        self.assertEqual(self.s.phone_code_hash, 'new')
        self.assertFalse(self.s.can_resend)
        with self.assertRaises(ValueError):
            await self.s.resend_code()

    async def test_resend_flood_wait_extends_cooldown(self):
        self.s.phone_code_hash = 'test-hash'
        self.s.can_resend = True
        self.s.client.side_effect = errors.FloodWaitError(None, capture=120)
        with self.assertRaises(errors.FloodWaitError):
            await self.s.resend_code()
        self.assertGreater(self.s.resend_at - time.monotonic(), 119)

    async def test_qr_wait_active_before_display_and_success(self):
        qr = QR()
        self.s.client.qr_login.return_value = qr
        def show(url):
            self.assertTrue(qr.waiting)
            self.assertEqual(url, qr.url)
            qr.accepted.set()
        self.s.qr_ready = show
        self.assertEqual((await self.s.qr_login('1', '0' * 32))['state'], 'ready')
        self.s._open_client.assert_awaited_once_with('1', '0' * 32, '', receive_updates=True)
        self.s.client.send_code_request.assert_not_awaited()

    async def test_qr_refreshes_after_expiry(self):
        qr = QR('refresh')
        self.s.client.qr_login.return_value = qr
        self.s.qr_ready = lambda _: qr.accepted.set()
        self.assertEqual((await self.s.qr_login('1', '0' * 32))['state'], 'ready')
        qr.recreate.assert_awaited_once()
        self.assertEqual(qr.calls, 2)

    async def test_qr_retains_two_factor_password_requirement(self):
        qr = QR('password')
        self.s.client.qr_login.return_value = qr
        self.s.qr_ready = lambda _: qr.accepted.set()
        self.assertEqual((await self.s.qr_login('1', '0' * 32))['state'], 'password')
        self.s._authorized.assert_not_awaited()

    async def test_cancel_qr_cleans_up_waiter(self):
        qr = QR()
        self.s.client.qr_login.return_value = qr
        task = asyncio.create_task(self.s.qr_login('1', '0' * 32))
        await qr.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(qr.waiting)
        self.s._authorized.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
