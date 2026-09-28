"""Windows desktop UI. Telegram operations stay on a dedicated asyncio thread."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import sys
import time
from pathlib import Path

import qrcode
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl, QLockFile
from PySide6.QtGui import QDesktopServices, QFont, QImage, QPixmap, QImageReader
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QPlainTextEdit, QGroupBox, QCheckBox,
    QMessageBox, QProgressBar, QDialog, QFileDialog, QScrollArea, QFrame,
)

from core import Service, friendly_error, desktop_link
from desktop import desktop_executable, save_desktop_executable, launch_desktop
from cards import ResultGrid
from preferences import load_pinned_channels, save_pinned_channels


def preview_pixmap(path):
    reader = QImageReader(path)
    reader.setDecideFormatFromContent(True)
    size = reader.size()
    if not size.isValid() or max(size.width(), size.height()) > 4096:
        return QPixmap()
    return QPixmap.fromImage(reader.read())


def qr_pixmap(url):
    qr = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    cells = qr.get_matrix()
    size = len(cells)
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    for y, row in enumerate(cells):
        for x, black in enumerate(row):
            if black:
                image.setPixel(x, y, 0xff000000)
    scale = max(1, 300 // size)
    return QPixmap.fromImage(image).scaled(size * scale, size * scale,
                                          Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)


class QRDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("用手机 Telegram 扫码登录")
        self.setModal(True)
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        hint = QLabel("打开已登录的手机 Telegram：\n设置 → 设备 → 连接桌面设备\n扫描下方二维码，并在手机上确认登录。")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(320, 320)
        self.image.setStyleSheet("background: white;")
        layout.addWidget(self.image)
        note = QLabel("二维码过期会自动刷新。请在手机上选择要登录的账号。")
        note.setWordWrap(True)
        layout.addWidget(note)
        cancel = QPushButton("取消扫码")
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel)

    def set_code(self, url):
        self.image.setPixmap(qr_pixmap(url))


def protect(data: bytes, decrypt=False) -> bytes:
    """Encrypt settings with Windows DPAPI, bound to the current Windows user."""
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("Windows 无法加密或解密设置。")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


class Worker(QThread):
    ready = Signal()
    result = Signal(str, object, str)
    progress = Signal(str)
    qr_code = Signal(str)

    def __init__(self, data_dir):
        super().__init__()
        self.data_dir = data_dir
        self.loop = None
        self.tasks = {}

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.service = Service(self.data_dir, self.progress.emit, self.qr_code.emit)
        self.ready.emit()
        try:
            self.loop.run_forever()
        finally:
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def submit(self, kind, method, **kwargs):
        async def execute():
            self.tasks[kind] = asyncio.current_task()
            try:
                value = await getattr(self.service, method)(**kwargs)
                self.result.emit(kind, value, "")
            except asyncio.CancelledError:
                value = {"channels": self.service.index.channels()} if kind == "sync" and self.service.index else None
                self.result.emit(kind, value, "cancelled")
            except Exception as exc:
                value = {"channels": self.service.index.channels()} if kind == "sync" and self.service.index else None
                if kind == "auth":
                    value = {"retry_after": max(0, int(self.service.resend_at - time.monotonic() + .999)),
                             "can_resend": self.service.can_resend}
                self.result.emit(kind, value, friendly_error(exc))
            finally:
                self.tasks.pop(kind, None)
        asyncio.run_coroutine_threadsafe(execute(), self.loop)

    def cancel(self, kind):
        def do_cancel():
            task = self.tasks.get(kind)
            if task:
                task.cancel()
        self.loop.call_soon_threadsafe(do_cancel)

    def shutdown(self):
        async def finish():
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self.service.close()
            finally:
                self.loop.call_soon(self.loop.stop)
        asyncio.run_coroutine_threadsafe(finish(), self.loop)


STYLE = """
QMainWindow, QWidget#root { background: #f3f6fb; color: #162c43; }
QWidget { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; }
QLabel#title { font-size: 25px; font-weight: 700; color: #132f4b; }
QLabel#subtitle, QLabel#muted { color: #62778a; }
QLabel#status { color: #244f75; padding: 10px; background: #e7f0fa; border-radius: 6px; }
QGroupBox { background: white; border: 1px solid #dbe4ed; border-radius: 9px; margin-top: 13px; padding: 16px 12px 12px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #284866; }
QLineEdit, QComboBox, QPlainTextEdit { background: white; border: 1px solid #cbd7e4; border-radius: 5px; padding: 7px; color: #18344d; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #2887d0; }
QPushButton { background: #edf3fa; color: #205377; border: 1px solid #d1deeb; border-radius: 5px; padding: 8px 12px; }
QPushButton:hover { background: #dceafb; }
QPushButton#primary { color: white; background: #227bc0; border: 1px solid #227bc0; font-weight: 600; }
QPushButton#primary:hover { background: #1967a3; }
QPushButton:disabled { color: #97a7b6; background: #eff2f6; border-color: #e1e6ed; }
QPushButton#primary:disabled { color: #97a7b6; background: #eff2f6; border-color: #e1e6ed; }
QScrollArea#resultGrid, QWidget#cardsContent, QScrollArea#sidebar, QWidget#sidebarContent { background: transparent; border: none; }
QFrame#videoCard { background: white; border: 2px solid #dce5ee; border-radius: 10px; }
QFrame#videoCard:hover { border-color: #92b9d8; }
QFrame#videoCard[selected="true"] { border-color: #227bc0; background: #f7fbff; }
QLabel#cardTitle { font-size: 12px; color: #234058; }
QLabel#cardMeta { font-size: 11px; color: #758697; }
QProgressBar { border: none; background: #dce8f5; border-radius: 2px; max-height: 4px; }
QProgressBar::chunk { background: #268bd2; }
"""


class Window(QMainWindow):
    def __init__(self, data_dir):
        super().__init__()
        self.data_dir = data_dir
        self.settings_path = data_dir / "settings.dpapi"
        self.pinned_channels = load_pinned_channels(data_dir)
        self.authenticated = False
        self.busy = False
        self.search_busy = False
        self.worker_ready = False
        self.closing = False
        self.closed = False
        self.page = 0
        self.page_size = 6
        self.total = 0
        self.rows = []
        self.auth_state = "connect"
        self.rebuilding = False
        self.can_resend = False
        self.resend_at = 0
        self.auth_method = "code"
        self.qr_dialog = None
        self.preview_generation = 0
        self.preview_jobs = {}
        self.preview_results = {}
        self.row_by_key = {}
        self.last_desktop_open = 0
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(120)
        self.preview_timer.timeout.connect(self.load_visible_previews)
        self.setWindowTitle("Telegram 视频检索")
        self.resize(1360, 920)
        self.setMinimumSize(1100, 780)
        self.setStyleSheet(STYLE)
        self.build_ui()
        self.load_settings()
        self.worker = Worker(data_dir)
        self.worker.ready.connect(self.on_ready)
        self.worker.result.connect(self.on_result)
        self.worker.progress.connect(self.status.setText)
        self.worker.qr_code.connect(self.show_qr)
        self.worker.finished.connect(self.on_closed)
        self.worker.start()
        self.resend_timer = QTimer(self)
        self.resend_timer.timeout.connect(self.update_controls)
        self.resend_timer.start(1000)

    def build_ui(self):
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(10)
        header = QHBoxLayout()
        header.addWidget(QLabel("Telegram 视频检索", objectName="title"))
        header.addSpacing(12)
        header.addWidget(QLabel("大图预览 · 双关键词检索", objectName="subtitle"))
        header.addStretch()
        layout.addLayout(header)
        body = QHBoxLayout()
        body.setSpacing(18)
        layout.addLayout(body, 1)
        sidebar = QScrollArea(objectName="sidebar")
        sidebar.setFixedWidth(264)
        sidebar.setWidgetResizable(True)
        sidebar.setFrameShape(QFrame.Shape.NoFrame)
        sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_content = QWidget(objectName="sidebarContent")
        sidebar.setWidget(sidebar_content)
        left = QVBoxLayout(sidebar_content)
        left.setContentsMargins(0, 0, 4, 0)
        left.setSpacing(12)
        body.addWidget(sidebar)
        account = QGroupBox("01  连接账号")
        form = QVBoxLayout(account)
        self.account_label = QLabel("首次使用需要 Telegram API 凭据")
        self.account_label.setWordWrap(True)
        form.addWidget(self.account_label)
        self.credentials = QWidget()
        credential_box = QVBoxLayout(self.credentials)
        credential_box.setContentsMargins(0, 0, 0, 0)
        fields = QFormLayout()
        fields.setVerticalSpacing(9)
        self.api_id = QLineEdit(placeholderText="数字 ID")
        self.api_hash = QLineEdit(placeholderText="32 位 API Hash")
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.phone = QLineEdit(placeholderText="+国家区号 手机号")
        for field in (self.api_id, self.api_hash, self.phone):
            field.setMinimumHeight(34)
        fields.addRow("API ID", self.api_id)
        fields.addRow("API Hash", self.api_hash)
        fields.addRow("手机号", self.phone)
        credential_box.addLayout(fields)
        self.proxy = QLineEdit(placeholderText="代理（可选）：socks5://127.0.0.1:1080")
        self.proxy.setToolTip("支持 socks5://主机:端口 或 http://主机:端口，也可包含用户名和密码。不会自动沿用 Telegram 的代理。")
        self.proxy.setEchoMode(QLineEdit.EchoMode.Password)
        credential_box.addWidget(self.proxy)
        self.remember = QCheckBox("在本机加密保存设置")
        self.remember.setChecked(True)
        credential_box.addWidget(self.remember)
        form.addWidget(self.credentials)
        self.qr_btn = QPushButton("扫码登录（无需验证码）", objectName="primary")
        self.qr_btn.clicked.connect(self.connect_qr)
        form.addWidget(self.qr_btn)
        self.connect_btn = QPushButton("连接 / 获取验证码", objectName="primary")
        self.connect_btn.clicked.connect(self.connect_account)
        form.addWidget(self.connect_btn)
        self.delivery_hint = QLabel()
        self.delivery_hint.setWordWrap(True)
        self.delivery_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.delivery_hint.setObjectName("muted")
        form.addWidget(self.delivery_hint)
        self.delivery_hint.hide()
        self.code = QLineEdit(placeholderText="Telegram 发来的登录验证码")
        self.password = QLineEdit(placeholderText="两步验证密码")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.verify_btn = QPushButton("验证并登录", objectName="primary")
        self.verify_btn.clicked.connect(self.verify)
        self.code.returnPressed.connect(self.verify)
        self.password.returnPressed.connect(self.verify)
        for widget in (self.code, self.password, self.verify_btn):
            form.addWidget(widget)
            widget.hide()
        self.resend_btn = QPushButton("未收到？尝试其他方式")
        self.resend_btn.clicked.connect(self.resend_code)
        form.addWidget(self.resend_btn)
        self.resend_btn.hide()
        self.logout_btn = QPushButton("退出 Telegram 登录")
        self.logout_btn.clicked.connect(self.logout)
        self.logout_btn.hide()
        form.addWidget(self.logout_btn)
        api_link = QPushButton("获取 API ID / API Hash ↗")
        api_link.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://my.telegram.org/apps")))
        form.addWidget(api_link)
        left.addWidget(account)
        right = QVBoxLayout()
        right.setSpacing(10)
        body.addLayout(right, 1)
        channels = QGroupBox("02  选择频道与更新索引")
        ch = QVBoxLayout(channels)
        self.channel = QComboBox()
        self.channel.setMinimumContentsLength(10)
        self.channel.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.channel.addItem("所有已索引频道", None)
        self.channel.currentIndexChanged.connect(self.channel_changed)
        ch.addWidget(self.channel)
        self.refresh_btn = QPushButton("刷新频道")
        self.refresh_btn.clicked.connect(self.refresh_channels)
        ch.addWidget(self.refresh_btn)
        self.pin_btn = QPushButton("置顶当前频道")
        self.pin_btn.clicked.connect(self.toggle_channel_pin)
        ch.addWidget(self.pin_btn)
        self.sync_btn = QPushButton("建立 / 继续索引", objectName="primary")
        self.sync_btn.clicked.connect(lambda: self.sync(False))
        self.rebuild_btn = QPushButton("完整重扫")
        self.rebuild_btn.setToolTip("重新扫描全部历史，更新被编辑的说明、移除已删除的视频。成功后替换索引。")
        self.rebuild_btn.clicked.connect(lambda: self.sync(True))
        self.stop_btn = QPushButton("停止")
        self.stop_btn.clicked.connect(lambda: self.worker.cancel("sync"))
        ch.addWidget(self.sync_btn)
        buttons = QHBoxLayout()
        buttons.addWidget(self.rebuild_btn)
        buttons.addWidget(self.stop_btn)
        ch.addLayout(buttons)
        self.channel_info = QLabel("登录后，刷新你已加入的频道。", objectName="muted")
        self.channel_info.setWordWrap(True)
        ch.addWidget(self.channel_info)
        left.addWidget(channels)
        note = QLabel("新搜索会清理上一轮预览缓存。\n只下载缩略图，不下载完整视频。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        left.addWidget(note)
        left.addStretch()
        desktop_btn = QPushButton("设置 Telegram Desktop 路径")
        desktop_btn.clicked.connect(self.choose_desktop)
        left.addWidget(desktop_btn)
        data_btn = QPushButton("打开本地数据目录")
        data_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir))))
        left.addWidget(data_btn)
        search = QGroupBox("03  搜索视频")
        box = QVBoxLayout(search)
        query_row = QHBoxLayout()
        self.query = QLineEdit(placeholderText="关键词 1")
        self.query.setMaxLength(150)
        self.query.returnPressed.connect(lambda: self.search(0))
        query_row.addWidget(self.query, 1)
        query_row.addWidget(QLabel("＋", objectName="muted"))
        self.second_query = QLineEdit(placeholderText="关键词 2（可留空）")
        self.second_query.setMaxLength(150)
        self.second_query.returnPressed.connect(lambda: self.search(0))
        query_row.addWidget(self.second_query, 1)
        self.search_btn = QPushButton("搜索", objectName="primary")
        self.search_btn.clicked.connect(lambda: self.search(0))
        query_row.addWidget(self.search_btn)
        box.addLayout(query_row)
        options = QHBoxLayout()
        self.field = QComboBox()
        for title, value in (("文件名 + 说明", "all"), ("只查文件名", "filename"), ("只查说明", "caption")):
            self.field.addItem(title, value)
        self.mode = QComboBox()
        self.mode.addItem("包含全部关键词", "all")
        self.mode.addItem("包含任一关键词", "any")
        self.query.textEdited.connect(self.reset_paging)
        self.second_query.textEdited.connect(self.reset_paging)
        self.field.currentIndexChanged.connect(self.reset_paging)
        self.mode.currentIndexChanged.connect(self.reset_paging)
        options.addWidget(self.field)
        options.addWidget(self.mode)
        self.preview_toggle = QCheckBox("加载预览图")
        self.preview_toggle.setChecked(True)
        self.preview_toggle.toggled.connect(self.toggle_previews)
        options.addWidget(self.preview_toggle)
        options.addStretch()
        self.page_size_choice = QComboBox()
        self.page_size_choice.addItem("每页 6 个", 6)
        self.page_size_choice.addItem("每页 4 个 · 更大预览", 4)
        self.page_size_choice.currentIndexChanged.connect(self.change_page_size)
        options.addWidget(self.page_size_choice)
        box.addLayout(options)
        box.addWidget(QLabel("默认两个词都要出现，位置和顺序不限；每个框也可用空格分隔更多词。", objectName="muted"))
        right.addWidget(search)
        self.result_info = QLabel("建立索引后即可搜索；关键词留空可浏览全部视频。", objectName="muted")
        self.result_info.setWordWrap(True)
        right.addWidget(self.result_info)
        self.results = ResultGrid()
        self.results.selection_changed.connect(self.show_detail)
        self.results.opened.connect(self.open_result)
        self.results.viewport_changed.connect(self.preview_timer.start)
        right.addWidget(self.results, 1)
        pager = QHBoxLayout()
        self.open_btn = QPushButton("桌面版打开 ↗")
        self.open_btn.clicked.connect(self.open_result)
        self.preview_btn = QPushButton("放大预览")
        self.preview_btn.clicked.connect(self.show_preview)
        self.browser_btn = QPushButton("浏览器打开")
        self.browser_btn.clicked.connect(self.open_in_browser)
        self.copy_btn = QPushButton("复制链接")
        self.copy_btn.clicked.connect(self.copy_link)
        self.prev_btn = QPushButton("上一页")
        self.prev_btn.clicked.connect(lambda: self.search(self.page - 1, fresh=False))
        self.next_btn = QPushButton("下一页")
        self.next_btn.clicked.connect(lambda: self.search(self.page + 1, fresh=False))
        pager.addWidget(self.open_btn)
        pager.addWidget(self.preview_btn)
        pager.addWidget(self.browser_btn)
        pager.addWidget(self.copy_btn)
        self.detail_btn = QPushButton("完整说明")
        self.detail_btn.setCheckable(True)
        self.detail_btn.toggled.connect(lambda checked: self.detail.setVisible(checked))
        pager.addWidget(self.detail_btn)
        pager.addStretch()
        pager.addWidget(self.prev_btn)
        pager.addWidget(self.next_btn)
        right.addLayout(pager)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFixedHeight(92)
        self.detail.setPlaceholderText("选中结果查看完整文件名、说明和消息链接。")
        right.addWidget(self.detail)
        self.detail.hide()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel("就绪 · 所有索引都保存在本机。", objectName="status")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.update_controls()

    def on_ready(self):
        self.worker_ready = True
        if self.closing:
            self.worker.shutdown()
        else:
            self.update_controls()

    def update_controls(self):
        idle = self.worker_ready and not self.busy and not self.closing
        self.credentials.setVisible(not self.authenticated)
        for w in (self.api_id, self.api_hash, self.phone, self.proxy, self.remember):
            w.setEnabled(idle and not self.authenticated)
        self.connect_btn.setEnabled(idle and not self.authenticated)
        self.qr_btn.setEnabled(idle and not self.authenticated)
        self.verify_btn.setEnabled(idle and not self.authenticated)
        remaining = max(0, int(self.resend_at - time.monotonic() + .999))
        self.resend_btn.setEnabled(idle and not self.authenticated and self.auth_state == "code"
                                   and self.can_resend and remaining == 0)
        self.resend_btn.setText(f"可重发（{remaining} 秒后）" if remaining else "未收到？尝试其他方式")
        if self.auth_state == "code" and remaining > 0:
            self.connect_btn.setEnabled(False)
        self.refresh_btn.setEnabled(idle and self.authenticated)
        self.logout_btn.setEnabled(idle and self.authenticated and not self.search_busy)
        selected = self.channel.currentData() is not None
        self.sync_btn.setEnabled(idle and self.authenticated and selected)
        self.rebuild_btn.setEnabled(idle and self.authenticated and selected)
        self.pin_btn.setEnabled(idle and self.authenticated and selected and not self.search_busy)
        self.pin_btn.setText("取消频道置顶" if self.channel.currentData() in self.pinned_channels else "置顶当前频道")
        self.channel.setEnabled(idle and self.authenticated and not self.search_busy)
        self.stop_btn.setEnabled(self.busy and self.syncing)
        searchable = self.authenticated and not self.search_busy and not self.closing
        for w in (self.query, self.second_query, self.field, self.mode, self.search_btn, self.page_size_choice):
            w.setEnabled(searchable)
        self.prev_btn.setEnabled(searchable and self.page > 0)
        self.next_btn.setEnabled(searchable and (self.page + 1) * self.page_size < self.total)
        chosen = 0 <= self.results.selected_index < len(self.rows)
        self.open_btn.setEnabled(chosen)
        self.copy_btn.setEnabled(chosen)
        selected = self.selected_result()
        key = (selected['channel_id'], selected['message_id']) if selected else None
        self.preview_btn.setEnabled(bool(key and self.preview_results.get(key, {}).get('status') == 'ready'))
        self.browser_btn.setEnabled(chosen)
        self.detail_btn.setEnabled(chosen)

    @property
    def syncing(self):
        return getattr(self, "_syncing", False)

    def start_job(self, kind, method, message, **kwargs):
        self.busy = True
        self._syncing = kind == "sync"
        self.status.setText(message)
        self.progress.show()
        self.update_controls()
        self.worker.submit(kind, method, **kwargs)

    def load_settings(self):
        if os.name != "nt" or not self.settings_path.exists():
            return
        try:
            data = json.loads(protect(self.settings_path.read_bytes(), decrypt=True))
            for key in ("api_id", "api_hash", "phone", "proxy"):
                getattr(self, key).setText(data.get(key, ""))
        except Exception:
            self.status.setText("未能读取加密设置，请重新填写。")

    def save_settings(self):
        if self.remember.isChecked() and os.name == "nt":
            data = {key: getattr(self, key).text().strip() for key in ("api_id", "api_hash", "phone", "proxy")}
            self.settings_path.write_bytes(protect(json.dumps(data).encode("utf-8")))
        else:
            self.settings_path.unlink(missing_ok=True)

    def connect_account(self):
        self.begin_auth("code")

    def connect_qr(self):
        self.begin_auth("qr")

    def begin_auth(self, method):
        if self.busy or self.authenticated:
            return
        try:
            self.save_settings()
        except Exception:
            QMessageBox.warning(self, "保存设置失败", "未能保存加密设置。请取消勾选保存设置后重试。")
            return
        self.code.clear()
        self.password.clear()
        self.code.hide()
        self.password.hide()
        self.verify_btn.hide()
        self.delivery_hint.hide()
        self.resend_btn.hide()
        self.can_resend = False
        self.resend_at = 0
        self.auth_state = "connect"
        self.auth_method = method
        self.start_job("auth", "qr_login" if method == "qr" else "connect", "正在连接 Telegram…",
                       **{key: getattr(self, key).text().strip() for key in ("api_id", "api_hash", "phone", "proxy")})

    def show_qr(self, url):
        if self.closing or not self.busy or self.auth_method != "qr":
            return
        if self.qr_dialog is None:
            self.qr_dialog = QRDialog(self)
            self.qr_dialog.rejected.connect(lambda: self.worker.cancel("auth"))
        self.qr_dialog.set_code(url)
        self.qr_dialog.show()
        self.status.setText("请用手机 Telegram 的“设置 → 设备 → 连接桌面设备”扫码，并确认登录。")

    def close_qr(self):
        if self.qr_dialog:
            self.qr_dialog.accept()
            self.qr_dialog.deleteLater()
            self.qr_dialog = None

    def resend_code(self):
        if not self.busy and self.can_resend:
            self.start_job("auth", "resend_code", "正在按 Telegram 指定的下一种方式重发验证码…")

    def verify(self):
        if self.busy or self.authenticated or self.auth_state not in {"code", "password"}:
            return
        if self.auth_state == "password":
            password = self.password.text()
            self.password.clear()
            if not password:
                return
            self.start_job("auth", "verify", "正在验证两步验证密码…", password=password)
        else:
            code = self.code.text().strip()
            self.code.clear()
            if code:
                self.start_job("auth", "verify", "正在验证登录验证码…", code=code)

    def logout(self):
        self.authenticated = False
        self.invalidate_previews()
        self.rows = []
        self.results.set_records([], self.page_size)
        self.detail.clear()
        self.total = self.page = 0
        self.start_job("logout", "logout", "正在退出 Telegram 登录…")

    def refresh_channels(self):
        if not self.busy:
            self.start_job("channels", "list_channels", "正在读取你已加入的频道…")

    def fill_channels(self, channels):
        old = self.channel.currentData()
        self.channel_data = {c["id"]: c for c in channels}
        self.channel.blockSignals(True)
        self.channel.clear()
        pinned = [c for c in channels if c['id'] in self.pinned_channels]
        for c in pinned:
            self.channel.addItem(f"★ {c['title']}  ·  {c['count']:,} 个视频", c["id"])
        self.channel.addItem("所有已索引频道", None)
        for c in channels:
            if c not in pinned:
                self.channel.addItem(f"{c['title']}  ·  {c['count']:,} 个视频", c["id"])
        index = self.channel.findData(old)
        self.channel.setCurrentIndex(max(0, index))
        self.channel.blockSignals(False)
        self.channel_changed()

    def toggle_channel_pin(self):
        channel_id = self.channel.currentData()
        if channel_id is None or not self.authenticated:
            return
        updated = self.pinned_channels ^ {channel_id}
        try:
            save_pinned_channels(self.data_dir, updated)
        except OSError:
            QMessageBox.warning(self, "保存失败", "无法保存频道置顶设置，请检查数据目录的写入权限。")
            return
        self.pinned_channels = updated
        self.fill_channels(list(self.channel_data.values()))
        self.status.setText("已置顶当前频道。" if channel_id in updated else "已取消当前频道置顶。")

    def channel_changed(self):
        self.invalidate_previews()
        self.rows = []
        self.results.set_records([], self.page_size)
        self.detail.clear()
        self.total = self.page = 0
        self.result_info.setText("输入关键词搜索；留空可浏览当前范围内的全部视频。")
        channel = getattr(self, "channel_data", {}).get(self.channel.currentData())
        if channel:
            updated = channel["updated"].replace("T", " ") or "尚未完成扫描"
            self.channel_info.setText(f"已索引 {channel['count']:,} 个视频 · 最近完成：{updated}")
        else:
            self.channel_info.setText("搜索全部已索引频道；建立索引时请先选择一个频道。")
        self.update_controls()

    def sync(self, rebuild):
        if self.busy or self.channel.currentData() is None:
            return
        self.rebuilding = rebuild
        self.start_job("sync", "sync", "正在完整重扫历史…" if rebuild else "正在建立 / 继续索引…",
                       channel_id=self.channel.currentData(), rebuild=rebuild)

    def search(self, page, fresh=True):
        if not self.authenticated or self.search_busy:
            return
        self.search_busy = True
        self.invalidate_previews()
        # Keep the row map until results arrive, but do not request old thumbnails.
        self.row_by_key = {(r['channel_id'], r['message_id']): i for i, r in enumerate(self.rows)}
        self.update_controls()
        self.worker.submit("search", "search", query=self.query.text(), channel_id=self.channel.currentData(),
                           second_query=self.second_query.text(), field=self.field.currentData(), mode=self.mode.currentData(),
                           page=max(0, page), page_size=self.page_size, fresh=fresh)

    def change_page_size(self):
        self.page_size = self.page_size_choice.currentData()
        self.reset_paging()
        if self.authenticated:
            self.search(0)

    def reset_paging(self, *_):
        self.page = self.total = 0
        self.result_info.setText("搜索条件已改变，请点击“搜索”或按回车更新结果。")
        self.update_controls()

    def on_result(self, kind, value, error):
        if self.closing:
            return
        if kind.startswith("thumbnail:"):
            self.on_thumbnail(kind, value, error)
            return
        if kind == "search":
            self.search_busy = False
        else:
            self.busy = self._syncing = False
            self.progress.hide()
        if kind == "auth":
            self.close_qr()
        if error:
            if kind == "auth" and value:
                self.resend_at = time.monotonic() + value.get("retry_after", 0)
                self.can_resend = value.get("can_resend", False)
            if kind == "sync" and value:
                self.fill_channels(value["channels"])
            if error == "cancelled":
                self.status.setText("已取消扫码登录。" if kind == "auth" else
                                    "已停止完整重扫，原索引保留。" if self.rebuilding else "已停止，已保存的进度可继续索引。")
            else:
                self.status.setText(error)
                QMessageBox.warning(self, "操作未完成", error)
            if kind == "logout":
                self.authenticated = True
            self.update_controls()
            return
        if kind == "auth":
            self.auth_state = value["state"]
            if self.auth_state == "code":
                self.can_resend = value.get("can_resend", False)
                self.resend_at = time.monotonic() + value.get("retry_after", 0)
                delivery = value.get("delivery", "请查看 Telegram 指定的接收方式，或改用扫码登录。")
                self.delivery_hint.setText(delivery + ("" if self.can_resend else "\n当前未提供其他重发方式，可改用扫码登录。"))
                self.delivery_hint.show()
                self.resend_btn.setVisible(self.can_resend)
                self.code.show()
                self.verify_btn.show()
                self.code.setFocus()
                self.status.setText(delivery)
            elif self.auth_state == "password":
                self.code.hide()
                self.resend_btn.hide()
                self.delivery_hint.hide()
                self.password.show()
                self.verify_btn.show()
                self.password.setFocus()
                self.status.setText("此账号开启了两步验证，请输入密码。")
            elif self.auth_state == "unsupported":
                self.code.hide()
                self.verify_btn.hide()
                self.delivery_hint.setText(value["delivery"])
                self.delivery_hint.show()
                self.status.setText(value["delivery"])
            else:
                self.authenticated = True
                self.account_label.setText(f"已登录：{value['name']}")
                self.account_label.setTextFormat(Qt.TextFormat.PlainText)
                for w in (self.code, self.password, self.verify_btn, self.connect_btn,
                          self.qr_btn, self.delivery_hint, self.resend_btn):
                    w.hide()
                self.logout_btn.show()
                self.fill_channels(value["channels"])
                self.refresh_channels()
        elif kind == "channels":
            self.fill_channels(value)
            self.status.setText(f"已加载 {len(value)} 个频道。选择频道后点击“建立 / 继续索引”。")
        elif kind == "sync":
            self.fill_channels(value["channels"])
            self.status.setText(f"索引完成 · 本次检查 {value['seen']:,} 条消息，发现 {value['found']:,} 个视频。")
            self.search(0)
        elif kind == "search":
            self.render_results(value)
        elif kind == "logout":
            self.channel_data = {}
            self.fill_channels([])
            self.connect_btn.show()
            self.qr_btn.show()
            self.logout_btn.hide()
            self.account_label.setText("已退出 Telegram 登录")
            self.auth_state = "connect"
            self.status.setText("已退出。历史索引保留在本机；重新登录同一账号可继续使用。")
        self.update_controls()

    def render_results(self, result):
        self.invalidate_previews()
        self.page, self.total, self.rows = result["page"], result["total"], result["rows"]
        self.page_size = result['page_size']
        for i, r in enumerate(self.rows):
            self.row_by_key[(r['channel_id'], r['message_id'])] = i
        self.detail.clear()
        self.results.set_records(self.rows, self.page_size)
        if not self.preview_toggle.isChecked():
            for card in self.results.cards:
                card.preview.set_preview(placeholder="预览已关闭")
        pages = max(1, (self.total + self.page_size - 1) // self.page_size)
        self.result_info.setText(f"找到 {self.total:,} 个视频 · 第 {self.page + 1} / {pages} 页 · 每页 {self.page_size} 个 · 双击卡片打开原消息" if self.total
                                 else "没有匹配的视频。可缩短关键词、改查说明，或先为该频道建立索引。")
        self.preview_timer.start()

    def invalidate_previews(self):
        self.preview_generation += 1
        self.preview_results.clear()
        self.row_by_key.clear()
        self.preview_timer.stop()
        for kind in list(self.preview_jobs):
            self.worker.cancel(kind)

    def toggle_previews(self, enabled):
        self.invalidate_previews()
        for row, record in enumerate(self.rows):
            self.row_by_key[(record['channel_id'], record['message_id'])] = row
            self.results.cards[row].preview.set_preview(placeholder="待加载" if enabled else "预览已关闭")
        self.preview_timer.start()
        self.update_controls()

    def load_visible_previews(self):
        if self.closing or self.search_busy or not self.worker_ready or not self.authenticated or not self.preview_toggle.isChecked():
            return
        visible = []
        selected = self.results.selected_index
        if 0 <= selected < len(self.rows):
            visible.append(selected)
        for row in self.results.visible_indices():
            if row not in visible:
                visible.append(row)
        for row in visible:
            if len(self.preview_jobs) >= 2:
                break
            record = self.rows[row]
            key = (record['channel_id'], record['message_id'])
            if key in self.preview_results:
                continue
            self.preview_results[key] = {"status": "loading"}
            self.results.cards[row].preview.set_preview(placeholder="加载中…")
            kind = f"thumbnail:{self.preview_generation}:{key[0]}:{key[1]}"
            self.preview_jobs[kind] = (self.preview_generation, key)
            self.worker.submit(kind, "thumbnail", channel_id=key[0], message_id=key[1])

    def on_thumbnail(self, kind, value, error):
        job = self.preview_jobs.pop(kind, None)
        if job and job[0] == self.preview_generation and job[1] in self.row_by_key:
            key = job[1]
            preview = self.results.cards[self.row_by_key[key]].preview
            result = value or {"status": "error"}
            if not error and result.get("status") == "ready":
                pixmap = preview_pixmap(result['path'])
                if not pixmap.isNull():
                    preview.set_preview(pixmap)
                else:
                    result = {"status": "missing"}
            if result.get('status') != 'ready' or error:
                preview.set_preview(placeholder={"missing": "此视频没有预览图", "rate_limited": "稍后重试"}.get(result.get('status'), "加载失败，重新搜索可重试"))
            self.preview_results[key] = result
        self.update_controls()
        self.preview_timer.start()

    def show_preview(self):
        record = self.selected_result()
        if not record:
            return
        result = self.preview_results.get((record['channel_id'], record['message_id']), {})
        if result.get('status') != 'ready':
            return
        pixmap = preview_pixmap(result['path'])
        if pixmap.isNull():
            return
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(record['filename'] or "视频预览图")
        box = QVBoxLayout(dialog)
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumSize(640, 360)
        label.setStyleSheet("background: #132f4b;")
        label.setPixmap(pixmap.scaled(640, 360, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        box.addWidget(label)
        note = QLabel("Telegram 提供的缩略图，清晰度取决于原视频附带的预览。")
        box.addWidget(note)
        dialog.show()

    def selected_result(self):
        row = self.results.selected_index
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def show_detail(self):
        r = self.selected_result()
        if r:
            self.detail.setPlainText(f"文件名：{r['filename'] or '未提供'}\n说明：{r['caption'] or '无'}\n{r['link']}")
        self.update_controls()
        self.preview_timer.start()

    def open_result(self):
        r = self.selected_result()
        if not r or time.monotonic() - self.last_desktop_open < 1:
            return
        try:
            executable = desktop_executable(self.data_dir)
            if not executable:
                self.choose_desktop()
                executable = desktop_executable(self.data_dir)
            if executable:
                launch_desktop(executable, desktop_link(r['channel_id'], r['username'], r['message_id']))
                self.last_desktop_open = time.monotonic()
                self.status.setText("已将视频链接交给 Telegram Desktop。请确保桌面版账号能访问该频道。")
        except (OSError, ValueError) as exc:
            self.status.setText("未能启动 Telegram Desktop，请检查桌面版路径，或使用“浏览器打开”。")
            QMessageBox.warning(self, "无法打开桌面版", str(exc))

    def choose_desktop(self):
        executable, _ = QFileDialog.getOpenFileName(self, "选择 Telegram Desktop 的 Telegram.exe",
                                                     desktop_executable(self.data_dir) or "", "Telegram Desktop (Telegram.exe)")
        if executable:
            try:
                save_desktop_executable(self.data_dir, executable)
                self.status.setText("已保存 Telegram Desktop 路径。")
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "路径设置失败", str(exc))

    def open_in_browser(self):
        r = self.selected_result()
        if r:
            QDesktopServices.openUrl(QUrl(r['link']))

    def copy_link(self):
        r = self.selected_result()
        if r:
            QApplication.clipboard().setText(r["link"])
            self.status.setText("已复制视频原消息链接。")

    def closeEvent(self, event):
        if self.closed:
            event.accept()
            return
        event.ignore()
        if not self.closing:
            self.closing = True
            self.close_qr()
            self.status.setText("正在保存进度并断开连接…")
            self.setEnabled(False)
            if self.worker_ready:
                self.worker.shutdown()

    def on_closed(self):
        self.closed = True
        self.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    data_dir = (args.data_dir or base / "data").resolve()
    app = QApplication(sys.argv[:1])
    app.setFont(QFont("Microsoft YaHei UI", 10))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(data_dir / "app.lock"))
        if not lock.tryLock(0):
            QMessageBox.information(None, "程序已打开", "同一数据目录只能打开一个程序，请切换到已打开的窗口。")
            return 1
        window = Window(data_dir)
    except Exception as exc:
        QMessageBox.critical(None, "启动失败", f"无法初始化本地数据，请将程序解压到有写入权限的目录。\n{type(exc).__name__}")
        return 1
    window.show()
    if args.smoke_test:
        def finish_smoke():
            if args.screenshot:
                window.grab().save(str(args.screenshot))
            window.close()
        QTimer.singleShot(1200, finish_smoke)
    result = app.exec()
    lock.unlock()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
