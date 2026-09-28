"""Local, account-scoped metadata index. No media downloads or message writes."""
from __future__ import annotations

import asyncio
import math
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote, urlencode

from telethon import TelegramClient, errors, types, functions


def code_delivery(sent):
    """Interpret Telegram's actual delivery route, without assuming arrival."""
    kind = type(getattr(sent, "type", None)).__name__
    hints = {
        "SentCodeTypeApp": "Telegram 指定：发到已登录设备中的「Telegram」官方通知会话。请核对账号，并检查已归档聊天。",
        "SentCodeTypeSms": "Telegram 指定：发到该手机号的短信，请查看手机短信和拦截记录。",
        "SentCodeTypeCall": "Telegram 指定：通过语音来电告知验证码，请留意该手机号的来电。",
        "SentCodeTypeFlashCall": "Telegram 指定：通过闪电来电验证，请输入来电号码。",
        "SentCodeTypeMissedCall": "Telegram 指定：通过未接来电验证，请输入来电号码的末尾验证码数字。",
        "SentCodeTypeSmsWord": "Telegram 指定：通过短信发送一个单词，请输入短信中的验证单词。",
        "SentCodeTypeSmsPhrase": "Telegram 指定：通过短信发送短语，请完整输入短信中的验证短语。",
        "SentCodeTypeFragmentSms": "Telegram 指定：通过 Fragment 接收验证码，请在自己的 Fragment 账号查看。也可以使用扫码登录。",
    }
    if kind == "SentCodeTypeEmailCode":
        hint = f"Telegram 指定：发到登录邮箱 {sent.type.email_pattern}，请查看收件箱和垃圾邮件。"
    else:
        hint = hints.get(kind)
    if hint is None:
        return {"state": "unsupported", "delivery": "Telegram 要求的这种验证方式暂未集成（例如邮箱设置或官方客户端验证）。请改用扫码登录，或先在官方手机应用完成验证。",
                "can_resend": False, "retry_after": 0}
    timeout = getattr(sent, "timeout", None)
    return {"state": "code", "delivery": hint, "can_resend": getattr(sent, "next_type", None) is not None,
            "retry_after": max(0, int(timeout)) if timeout is not None else 60}


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold()


def parse_proxy(value: str):
    if not value.strip():
        return None
    p = urlparse(value.strip())
    if p.scheme not in {"socks5", "http"} or not p.hostname or not p.port:
        raise ValueError("代理格式：socks5://127.0.0.1:1080 或 http://127.0.0.1:7890")
    return dict(proxy_type=p.scheme, addr=p.hostname, port=p.port, rdns=True,
                username=unquote(p.username) if p.username else None,
                password=unquote(p.password) if p.password else None)


def message_link(channel_id: int, username: str, message_id: int) -> str:
    return f"https://t.me/{username}/{message_id}" if username else f"https://t.me/c/{channel_id}/{message_id}"


def desktop_link(channel_id: int, username: str, message_id: int) -> str:
    if int(channel_id) <= 0 or int(message_id) <= 0:
        raise ValueError("无效的频道或消息编号。")
    if username:
        return "tg://resolve?" + urlencode({"domain": username, "post": int(message_id)})
    return "tg://privatepost?" + urlencode({"channel": int(channel_id), "post": int(message_id)})


def static_thumbnail(document):
    # Never choose VideoSize or fall back to the full document download.
    candidates = [t for t in (getattr(document, "thumbs", None) or [])
                  if isinstance(t, (types.PhotoSize, types.PhotoCachedSize, types.PhotoStrippedSize))
                  and getattr(t, "size", len(getattr(t, "bytes", b""))) <= 2 * 1024 * 1024]
    return max(candidates, key=lambda t: (getattr(t, "w", 0) * getattr(t, "h", 0),
                                         getattr(t, "size", len(getattr(t, "bytes", b"")))) , default=None)


def video_record(message, channel_id: int):
    doc = getattr(message, "document", None)
    if not doc:
        return None
    attrs = doc.attributes or []
    name = next((a.file_name for a in attrs if isinstance(a, types.DocumentAttributeFilename)), "")
    mime = (doc.mime_type or "").lower()
    ext = Path(name).suffix.lower()
    if not (mime.startswith("video/") or any(isinstance(a, types.DocumentAttributeVideo) for a in attrs)
            or ext in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".mpg", ".mpeg", ".wmv", ".flv", ".3gp"}):
        return None
    caption = message.message or ""
    duration = next((int(a.duration) for a in attrs if isinstance(a, types.DocumentAttributeVideo)), 0)
    date = message.date.astimezone(timezone.utc).isoformat() if message.date else ""
    return (channel_id, message.id, name, caption, normalize(name), normalize(caption), date, duration, doc.size or 0)


class Index:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
                cursor INTEGER NOT NULL DEFAULT 0, updated TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS videos (
                channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                filename TEXT NOT NULL, caption TEXT NOT NULL,
                norm_name TEXT NOT NULL, norm_caption TEXT NOT NULL,
                date TEXT NOT NULL, duration INTEGER NOT NULL, size INTEGER NOT NULL,
                PRIMARY KEY(channel_id, message_id));
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def channel(self, channel_id, title, username):
        with self.db:
            self.db.execute("""INSERT INTO channels(id,title,username) VALUES(?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, username=excluded.username""",
                (channel_id, title, username or ""))

    def channels(self):
        return [dict(r) for r in self.db.execute("""SELECT c.*, COUNT(v.message_id) AS count
            FROM channels c LEFT JOIN videos v ON c.id=v.channel_id GROUP BY c.id ORDER BY c.title""")]

    def cursor(self, channel_id):
        r = self.db.execute("SELECT cursor FROM channels WHERE id=?", (channel_id,)).fetchone()
        return r[0] if r else 0

    def save_batch(self, records, channel_id, cursor, complete=False):
        # Checkpoint and data commit together: interrupted sync cannot skip videos.
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO videos VALUES(?,?,?,?,?,?,?,?,?)", records)
            self.db.execute("UPDATE channels SET cursor=MAX(cursor,?) WHERE id=?", (cursor, channel_id))
            if complete:
                self.db.execute("UPDATE channels SET updated=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), channel_id))

    def begin_rebuild(self):
        self.db.execute("DROP TABLE IF EXISTS temp.scan_videos")
        self.db.execute("CREATE TEMP TABLE scan_videos AS SELECT * FROM videos WHERE 0")

    def stage(self, records):
        with self.db:
            self.db.executemany("INSERT INTO scan_videos VALUES(?,?,?,?,?,?,?,?,?)", records)

    def finish_rebuild(self, channel_id, cursor):
        with self.db:
            self.db.execute("DELETE FROM videos WHERE channel_id=?", (channel_id,))
            self.db.execute("INSERT INTO videos SELECT * FROM scan_videos")
            self.db.execute("UPDATE channels SET cursor=?,updated=? WHERE id=?",
                            (cursor, datetime.now().isoformat(timespec="seconds"), channel_id))
        self.db.execute("DROP TABLE scan_videos")

    def search(self, query="", channel_id=None, field="all", mode="all", page=0, page_size=50, second_query=""):
        terms = normalize(f"{query} {second_query}").split()
        if len(query) + len(second_query) > 300 or len(terms) > 12:
            raise ValueError("最多输入 300 个字符、12 个关键词。")
        if field not in {"all", "filename", "caption"} or mode not in {"all", "any"}:
            raise ValueError("搜索选项无效。")
        where, params = [], []
        if channel_id is not None:
            where.append("v.channel_id=?")
            params.append(channel_id)
        parts = []
        for term in terms:
            # instr uses literal substring matching (%, _, quotes are not SQL operators).
            if field == "all":
                parts.append("(instr(v.norm_name,?)>0 OR instr(v.norm_caption,?)>0)")
                params.extend([term, term])
            else:
                col = "norm_name" if field == "filename" else "norm_caption"
                parts.append(f"instr(v.{col},?)>0")
                params.append(term)
        if parts:
            where.append("(" + (" AND " if mode == "all" else " OR ").join(parts) + ")")
        clause = " WHERE " + " AND ".join(where) if where else ""
        source = " FROM videos v JOIN channels c ON c.id=v.channel_id" + clause
        total = self.db.execute("SELECT COUNT(*)" + source, params).fetchone()[0]
        rows = self.db.execute("SELECT v.*,c.title AS channel_title,c.username" + source +
                               " ORDER BY v.date DESC,v.channel_id,v.message_id DESC LIMIT ? OFFSET ?",
                               params + [page_size, max(0, page) * page_size]).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            r["link"] = message_link(r["channel_id"], r["username"], r["message_id"])
            result.append(r)
        return dict(total=total, rows=result, page=page, page_size=page_size)


def friendly_error(exc):
    if isinstance(exc, errors.FloodWaitError):
        return f"Telegram 要求等待 {exc.seconds} 秒后重试。索引进度已保留。"
    messages = {
        "ApiIdInvalidError": "API ID 或 API Hash 不正确，请检查。",
        "PhoneNumberInvalidError": "手机号不正确，请使用带国家区号的格式，例如 +8613800000000。",
        "PhoneCodeInvalidError": "验证码不正确，请重新输入。",
        "PhoneCodeExpiredError": "验证码已过期，请重新连接并获取验证码。",
        "PasswordHashInvalidError": "两步验证密码不正确，请重试。",
        "SendCodeUnavailableError": "Telegram 暂不允许再次发送验证码。请稍后重试，或使用扫码登录。",
        "PhoneNumberFloodError": "该号码的验证码请求过于频繁。请停止重发，等待 Telegram 解除限制。",
        "AuthTokenExpiredError": "二维码已过期，请重新点击扫码登录。",
        "ChannelPrivateError": "当前账号无法读取该频道，请先在 Telegram 中确认访问权限。",
        "AuthKeyUnregisteredError": "登录状态已失效，请重新登录。",
        "SessionRevokedError": "该登录会话已被撤销，请重新登录。",
        "SessionPasswordNeededError": "请填写 Telegram 两步验证密码。",
    }
    if type(exc).__name__ in messages:
        return messages[type(exc).__name__]
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return "连接失败或超时，请检查网络；需要代理时填写 SOCKS5 / HTTP 代理地址后重新连接。"
    if isinstance(exc, ValueError):
        return str(exc)
    # Do not expose raw network errors containing proxy credentials.
    return f"操作失败（{type(exc).__name__}）。请检查设置后重试。"


class Service:
    def __init__(self, data_dir: Path, progress=lambda _: None, qr_ready=lambda _: None):
        self.data_dir = data_dir
        self.progress = progress
        self.client = None
        self.index = None
        self.entities = {}
        self.phone = ""
        self.phone_code_hash = None
        self.qr_ready = qr_ready
        self.resend_at = 0
        self.can_resend = False
        self.account_id = None
        self.thumbnail_retry_at = 0
        self.thumbnail_tasks = set()

    async def _open_client(self, api_id, api_hash, proxy="", receive_updates=False):
        if not str(api_id).isdigit() or int(api_id) <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash.strip()):
            raise ValueError("请填写有效的 API ID（数字）和 API Hash（32 位字符）。")
        proxy_config = parse_proxy(proxy)
        await self.close()
        self.entities = {}
        self.phone_code_hash = None
        self.can_resend = False
        self.resend_at = 0
        self.client = TelegramClient(str(self.data_dir / "telegram"), int(api_id), api_hash.strip(),
                                     proxy=proxy_config, timeout=15, connection_retries=2,
                                     request_retries=2, flood_sleep_threshold=0,
                                     receive_updates=receive_updates, device_model="Local Video Search")
        await asyncio.wait_for(self.client.connect(), 45)

    async def connect(self, api_id, api_hash, phone, proxy=""):
        await self._open_client(api_id, api_hash, proxy)
        if await self.client.is_user_authorized():
            return await self._authorized()
        self.phone = re.sub(r"[\s()-]", "", phone)
        if not re.fullmatch(r"\+\d{6,16}", self.phone):
            raise ValueError("首次登录需要填写以 + 开头、包含国家区号的完整手机号。")
        try:
            sent = await self.client.send_code_request(self.phone)
        except errors.SessionPasswordNeededError:
            return {"state": "password"}
        return self._code_response(sent)

    def _code_response(self, sent):
        result = code_delivery(sent)
        self.phone_code_hash = getattr(sent, "phone_code_hash", None)
        self.can_resend = result["can_resend"] and bool(self.phone_code_hash)
        self.resend_at = time.monotonic() + result["retry_after"]
        return result

    async def resend_code(self):
        if not self.client or not self.phone_code_hash or not self.can_resend:
            raise ValueError("Telegram 当前未提供可切换的验证码接收方式，请使用扫码登录。")
        remaining = math.ceil(self.resend_at - time.monotonic())
        if remaining > 0:
            raise ValueError(f"请等待 {remaining} 秒，再按 Telegram 允许的方式重发。")
        try:
            sent = await self.client(functions.auth.ResendCodeRequest(self.phone, self.phone_code_hash))
        except errors.SessionPasswordNeededError:
            return {"state": "password"}
        except errors.FloodWaitError as exc:
            self.resend_at = time.monotonic() + exc.seconds
            raise
        return self._code_response(sent)

    async def qr_login(self, api_id, api_hash, phone="", proxy=""):
        # QR login needs the UpdateLoginToken notification; normal indexing does not.
        await self._open_client(api_id, api_hash, proxy, receive_updates=True)
        if await self.client.is_user_authorized():
            return await self._authorized()
        try:
            qr = await self.client.qr_login()
            for attempt in range(10):
                waiter = asyncio.create_task(qr.wait())
                try:
                    # Register the waiter before showing the scannable token.
                    await asyncio.sleep(0)
                    self.qr_ready(qr.url)
                    await waiter
                    return await self._authorized()
                except asyncio.TimeoutError:
                    if attempt < 9:
                        self.progress("二维码已到期，正在自动刷新…")
                        await qr.recreate()
                finally:
                    if not waiter.done():
                        waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
        except errors.SessionPasswordNeededError:
            return {"state": "password"}
        raise ValueError("等待扫码已超时，请重新点击“扫码登录”。")

    async def verify(self, code="", password=""):
        if not self.client:
            raise ValueError("请先连接并获取验证码。")
        try:
            if password:
                await self.client.sign_in(password=password)
            else:
                await self.client.sign_in(phone=self.phone, code=code.strip(), phone_code_hash=self.phone_code_hash)
        except errors.SessionPasswordNeededError:
            return {"state": "password"}
        return await self._authorized()

    async def _authorized(self):
        me = await self.client.get_me()
        if self.index:
            self.index.close()
        self.index = Index(self.data_dir / f"index_{me.id}.sqlite3")
        self.account_id = me.id
        return {"state": "ready", "name": me.first_name or me.username or str(me.id), "channels": self.index.channels()}

    async def list_channels(self):
        if not self.index:
            raise ValueError("请先登录。")
        count = 0
        async for dialog in self.client.iter_dialogs():
            e = dialog.entity
            if isinstance(e, types.Channel) and e.broadcast:
                self.entities[e.id] = e
                self.index.channel(e.id, e.title, e.username)
                count += 1
                if count % 20 == 0:
                    self.progress(f"已读取 {count} 个频道…")
        return self.index.channels()

    async def sync(self, channel_id, rebuild=False):
        if not self.index:
            raise ValueError("请先登录。")
        entity = self.entities.get(channel_id)
        if entity is None:
            entity = await self.client.get_entity(types.PeerChannel(channel_id))
        self.index.channel(entity.id, entity.title, entity.username)
        cursor = 0 if rebuild else self.index.cursor(channel_id)
        seen = found = 0
        batch = []
        if rebuild:
            self.index.begin_rebuild()
        try:
            # Scan unfiltered history so videos uploaded as generic files are also found.
            async for msg in self.client.iter_messages(entity, limit=None, min_id=cursor, reverse=True, wait_time=1):
                record = video_record(msg, channel_id)
                if record:
                    batch.append(record)
                    found += 1
                cursor = max(cursor, msg.id)
                seen += 1
                if seen % 100 == 0:
                    if rebuild:
                        self.index.stage(batch)
                    else:
                        self.index.save_batch(batch, channel_id, cursor)
                    batch = []
                    self.progress(f"已检查 {seen:,} 条消息 · 发现 {found:,} 个视频 · 可随时停止")
                    await asyncio.sleep(0)
            if rebuild:
                self.index.stage(batch)
                self.index.finish_rebuild(channel_id, cursor)
            else:
                self.index.save_batch(batch, channel_id, cursor, complete=True)
            batch = []
            return {"seen": seen, "found": found, "channels": self.index.channels()}
        finally:
            if not rebuild and batch:
                self.index.save_batch(batch, channel_id, cursor)
            if rebuild:
                self.index.db.execute("DROP TABLE IF EXISTS temp.scan_videos")

    async def search(self, fresh=False, **kwargs):
        if not self.index:
            raise ValueError("请先登录并建立频道索引。")
        if fresh:
            await self.clear_thumbnails()
        return self.index.search(**kwargs)

    async def thumbnail(self, channel_id, message_id):
        task = asyncio.current_task()
        self.thumbnail_tasks.add(task)
        try:
            return await self._thumbnail(channel_id, message_id)
        finally:
            self.thumbnail_tasks.discard(task)

    async def clear_thumbnails(self):
        pending = list(self.thumbnail_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self.account_id is None:
            return
        base = (self.data_dir / "thumbnails").resolve()
        cache = base / str(int(self.account_id))
        if not cache.exists():
            return
        if cache.is_symlink() or cache.resolve().parent != base:
            raise ValueError("预览缓存目录位置异常，未清理。")
        try:
            for path in cache.iterdir():
                if re.fullmatch(r"\d+_\d+\.(jpg|tmp)", path.name) and path.is_file() and not path.is_symlink():
                    path.unlink()
        except OSError as exc:
            raise ValueError("上一轮预览缓存无法清理，请关闭正在打开缓存图片的其他程序后重试。") from exc

    async def _thumbnail(self, channel_id, message_id):
        if not self.client or not self.index or self.account_id is None:
            raise ValueError("请先登录后加载预览图。")
        channel_id, message_id = int(channel_id), int(message_id)
        if channel_id <= 0 or message_id <= 0:
            raise ValueError("无效的预览图编号。")
        result = {"channel_id": channel_id, "message_id": message_id, "path": "", "status": "missing"}
        cache = self.data_dir / "thumbnails" / str(self.account_id)
        path = cache / f"{channel_id}_{message_id}.jpg"
        if path.is_file() and 0 < path.stat().st_size <= 2 * 1024 * 1024 and time.time() - path.stat().st_mtime < 86400:
            return {**result, "path": str(path), "status": "ready"}
        if time.monotonic() < self.thumbnail_retry_at:
            return {**result, "status": "rate_limited"}
        client = self.client
        try:
            # UI limits concurrency to two and only requests visible/selected rows.
            await asyncio.sleep(.2)
            entity = self.entities.get(channel_id) or types.PeerChannel(channel_id)
            msg = await asyncio.wait_for(client.get_messages(entity, ids=message_id), 20)
            document = getattr(msg, "document", None)
            thumb = static_thumbnail(document)
            if not document or thumb is None:
                return result
            blob = await asyncio.wait_for(client.download_media(msg, file=bytes, thumb=thumb), 25)
            if not isinstance(blob, bytes) or not blob or len(blob) > 2 * 1024 * 1024:
                return result
            cache.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            try:
                temporary.write_bytes(blob)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            return {**result, "path": str(path), "status": "ready"}
        except errors.FloodWaitError as exc:
            self.thumbnail_retry_at = time.monotonic() + exc.seconds
            return {**result, "status": "rate_limited"}

    async def logout(self):
        if self.client:
            await self.client.log_out()
        await self.close()
        self.entities = {}
        return True

    async def close(self):
        if self.client:
            await self.client.disconnect()
            self.client = None
        if self.index:
            self.index.close()
            self.index = None
        self.account_id = None
