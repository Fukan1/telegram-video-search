"""Preview-first result cards, with a fixed four/six-item page."""
from PySide6.QtCore import Qt, Signal, QPoint, QRect
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QVBoxLayout, QGridLayout, QScrollArea, QSizePolicy,
)


class PreviewCanvas(QWidget):
    def __init__(self):
        super().__init__()
        self.pixmap = QPixmap()
        self.placeholder = "待加载"
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_preview(self, pixmap=None, placeholder="待加载"):
        self.pixmap = pixmap if pixmap is not None else QPixmap()
        self.placeholder = placeholder
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#e9eff5"))
        painter.drawRoundedRect(self.rect(), 7, 7)
        if self.pixmap.isNull():
            painter.setPen(QColor("#718398"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
            return
        area = self.rect().adjusted(3, 3, -3, -3)
        scaled = self.pixmap.scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap(area.center() - scaled.rect().center(), scaled)


class VideoCard(QFrame):
    selected = Signal(int)
    opened = Signal(int)

    def __init__(self, index, record):
        super().__init__()
        self.index = index
        self.record = record
        self.setObjectName("videoCard")
        self.setProperty("selected", False)
        self.setMinimumWidth(210)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        box = QVBoxLayout(self)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(4)
        self.preview = PreviewCanvas()
        box.addWidget(self.preview, 1)
        title = record['filename'] or (record['caption'].splitlines() or ["未命名视频"])[0]
        self.title = QLabel(title)
        self.title.setObjectName("cardTitle")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setFixedHeight(34)
        self.title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        box.addWidget(self.title)
        self.meta = QLabel()
        self.meta.setObjectName("cardMeta")
        self.meta.setTextFormat(Qt.TextFormat.PlainText)
        self.meta.setFixedHeight(16)
        self.meta.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.meta.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        box.addWidget(self.meta)
        self.setToolTip(f"{title}\n{record['caption']}\n{record['channel_title']}\n双击在 Telegram Desktop 中打开")

    def set_selected(self, selected):
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        text = f"{self.record['date'][:10]}  ·  {self.record['size'] / 1024 / 1024:.1f} MB  ·  {self.record['channel_title']}"
        self.meta.setText(self.meta.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max(1, self.width() - 20)))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.index)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.index)
            self.opened.emit(self.index)
        super().mouseDoubleClickEvent(event)


class ResultGrid(QScrollArea):
    selection_changed = Signal()
    opened = Signal()
    viewport_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("resultGrid")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.content = QWidget(objectName="cardsContent")
        self.grid = QGridLayout(self.content)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(12)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self.content)
        self.cards = []
        self.selected_index = -1
        self.columns = 3
        self.empty = QLabel("搜索后，视频预览会显示在这里。", self.content)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setObjectName("muted")
        self.grid.addWidget(self.empty, 0, 0)
        self.verticalScrollBar().valueChanged.connect(lambda _: self.viewport_changed.emit())

    def set_records(self, records, page_size=6):
        for card in self.cards:
            self.grid.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = []
        self.selected_index = -1
        self.columns = 2 if page_size == 4 else 3
        for col in range(3):
            self.grid.setColumnStretch(col, 1 if col < self.columns else 0)
            self.grid.setColumnMinimumWidth(col, 0)
        self.empty.setVisible(not records)
        for i, record in enumerate(records):
            card = VideoCard(i, record)
            card.selected.connect(self.select)
            card.opened.connect(lambda _: self.opened.emit())
            self.cards.append(card)
            self.grid.addWidget(card, i // self.columns, i % self.columns)
        self.verticalScrollBar().setValue(0)
        self.resize_cards()
        if self.cards:
            self.select(0)

    def select(self, index):
        if not 0 <= index < len(self.cards):
            return
        self.selected_index = index
        for i, card in enumerate(self.cards):
            card.set_selected(i == index)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.selection_changed.emit()

    def visible_indices(self):
        return [i for i, card in enumerate(self.cards)
                if QRect(card.mapTo(self.viewport(), QPoint()), card.size()).intersects(self.viewport().rect())]

    def resize_cards(self):
        height = max(240, (self.viewport().height() - 12) // 2)
        for card in self.cards:
            card.setFixedHeight(height)
        self.empty.setMinimumHeight(max(240, self.viewport().height() - 8))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'cards'):
            self.resize_cards()
            self.viewport_changed.emit()

    def keyPressEvent(self, event):
        delta = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1,
                 Qt.Key.Key_Up: -self.columns, Qt.Key.Key_Down: self.columns}.get(event.key())
        if delta is not None and self.cards:
            self.select(max(0, min(len(self.cards) - 1, self.selected_index + delta)))
            self.ensureWidgetVisible(self.cards[self.selected_index])
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.selected_index >= 0:
            self.opened.emit()
        else:
            super().keyPressEvent(event)
