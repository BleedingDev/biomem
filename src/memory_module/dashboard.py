# biomem desktop dashboard.

__doc__ = '\nbiomem Dashboard — graphical interface (PyQt6 redesign).\n\nTabs (in order):\n  1. Chat         — main conversation interface\n  2. Module       — status, memory stats, backup/restore/shutdown\n  3. Memory       — clear STM/LTM, migrate legacy .pt\n  4. LLM Settings — API keys, model names, personalization, context limit\n  5. News         — messages from the backend (Markdown)\n\nArchitectural principles:\n  - GUI runs on the MainThread (QApplication exec)\n  - AsyncIO + WS server run in a background daemon (threading.Thread)\n  - Communication via signals and thread-safe QTimer\n  - Closing the window (X) MUST NOT quit the module — it only hides the window\n  - Quitting the module only via the button or the system tray\n'

import sys
import math
import logging
import threading
from typing import Optional, Callable, Dict, Any, List, TypedDict
from pathlib import Path
import asyncio

try:
    from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                                 QLabel, QLineEdit, QPushButton, QProgressBar, QTextEdit, QFrame,
                                 QMessageBox, QFileDialog, QTabWidget, QGridLayout, QSpacerItem,
                                 QSizePolicy, QGraphicsDropShadowEffect, QListWidget, QListWidgetItem,
                                 QComboBox, QScrollArea, QDialog, QDialogButtonBox, QSplitter,
                                 QSlider, QFormLayout, QAbstractButton, QToolTip, QGraphicsView,
                                 QGraphicsScene, QGraphicsItem, QGraphicsEllipseItem, QGraphicsLineItem,
                                 QGraphicsTextItem)
    from PyQt6.QtCore import (Qt, QTimer, pyqtSignal, QObject, QThread, QPoint, QSize, QUrl,
                              QPropertyAnimation, QEasingCurve, pyqtProperty, QRectF, QPointF)
    from PyQt6.QtGui import (QIcon, QFont, QColor, QPalette, QBrush, QCursor, QPixmap,
                             QDesktopServices, QPainter, QPen, QPainterPath, QLinearGradient,
                             QRadialGradient, QPolygonF)
except ImportError as e:
    raise ImportError('PyQt6 not found. Run: pip install PyQt6') from e

from .localization import T

logger = logging.getLogger('bdbm.dashboard')

MSG_STATUS_UPDATE = 'status_update'
MSG_MEMORY_STATS = 'memory_stats'
MSG_SERVER_READY = 'server_ready'
MSG_BACKUP_DONE = 'backup_done'
MSG_RESTORE_DONE = 'restore_done'
MSG_NEWS_LOADED = 'news_loaded'
MSG_EXPORT_DONE = 'export_done'
MSG_REPORT_DONE = 'report_done'
MSG_CONV_HANDLER_READY = 'conv_handler_ready'
MSG_REFACTOR_DONE = 'refactor_done'
MSG_REFACTOR_PROGRESS = 'refactor_progress'


class DashboardMessagePayload(TypedDict, total=False):
    """Thread-safe payload shared by background workers and the Qt dashboard."""

    text: str
    detail: str
    color: str
    success: bool
    error: str
    status: str
    path: str
    content: str
    news_id: str
    ltm_active: int
    ltm_total: int
    stm_active: int
    stm_total: int
    writes: int
    reads: int
    fatigue_pct: float
    command: str
    step: str
    current: int
    total: int

_COLORS = {
    'bg': '#f8fafc',
    'bg_card': '#ffffff',
    'bg_panel': '#ffffff',
    'bg_input': '#f1f5f9',
    'accent': '#0ea5e9',
    'accent_hover': '#0284c7',
    'accent_fg': '#ffffff',
    'success': '#10b981',
    'warning': '#f59e0b',
    'error': '#ef4444',
    'text': '#1e293b',
    'text_dim': '#475569',
    'text_white': '#0f172a',
    'border': '#e2e8f0',
    'border_glow': '#bae6fd',
    'btn_danger': '#ef4444',
    'btn_danger_hover': '#dc2626',
    'progress_bg': '#e2e8f0',
    'progress_fg': '#0ea5e9',
    'banner_bg': '#fef3c7',
    'banner_fg': '#92400e',
    'chat_user_bg': '#0ea5e9',
    'chat_user_fg': '#ffffff',
    'chat_model_bg': '#f1f5f9',
    'chat_model_fg': '#1e293b',
    'chat_sys_fg': '#94a3b8',
    'sidebar_bg': '#f8fafc',
    'sidebar_border': '#e2e8f0',
}

STYLESHEET = ''.join([
    '\nQMainWindow, QDialog {\n    background-color: ',
    f'{_COLORS["bg"]}',
    ';\n    color: ',
    f'{_COLORS["text"]}',
    ';\n}\nQToolTip {\n    background-color: #ffffff;\n    color: #1e293b;\n    border: 1px solid #cbd5e1;\n    border-radius: 6px;\n    padding: 6px 10px;\n    font-size: 12px;\n}\nQWidget {\n    color: ',
    f'{_COLORS["text_white"]}',
    ";\n    font-family: 'Inter', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;\n}\nQTabWidget::pane {\n    border: 0;\n    background: transparent;\n}\nQTabBar::tab {\n    background: transparent;\n    color: ",
    f'{_COLORS["text_dim"]}',
    ';\n    padding: 10px 20px;\n    font-size: 14px;\n    font-weight: 600;\n    margin-right: 8px;\n    border-bottom: 3px solid transparent;\n}\nQTabBar::tab:selected {\n    color: ',
    f'{_COLORS["accent"]}',
    ';\n    border-bottom: 3px solid ',
    f'{_COLORS["accent"]}',
    ';\n}\nQTabBar::tab:hover {\n    color: ',
    f'{_COLORS["text"]}',
    ';\n}\nQLineEdit {\n    background-color: ',
    f'{_COLORS["bg_input"]}',
    ';\n    border: 1px solid ',
    f'{_COLORS["border"]}',
    ';\n    border-radius: 8px;\n    padding: 10px 14px;\n    color: ',
    f'{_COLORS["text_white"]}',
    ';\n    font-size: 14px;\n}\nQLineEdit:focus {\n    background-color: #ffffff;\n    border: 2px solid ',
    f'{_COLORS["accent"]}',
    ';\n}\nQTextEdit {\n    background-color: transparent;\n    border: none;\n    color: ',
    f'{_COLORS["text"]}',
    ';\n}\nQPushButton {\n    border-radius: 8px;\n    font-size: 14px;\n    font-weight: 600;\n    padding: 10px 20px;\n    background-color: #ffffff;\n    color: #1e293b;\n    border: 1px solid #e2e8f0;\n}\nQPushButton:hover {\n    background-color: #f1f5f9;\n    border: 1px solid #bae6fd;\n}\nQPushButton#actionBtn {\n    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0ea5e9, stop:1 #0284c7);\n    color: #ffffff;\n    border: none;\n}\nQPushButton#actionBtn:hover {\n    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #38bdf8, stop:1 #0369a1);\n}\nQPushButton#dangerBtn {\n    background-color: #fee2e2;\n    color: #b91c1c;\n    border: 1px solid #fca5a5;\n}\nQPushButton#dangerBtn:hover {\n    background-color: #fca5a5;\n    color: #7f1d1d;\n}\nQPushButton#warningBtn {\n    background-color: #fef3c7;\n    color: #b45309;\n    border: 1px solid #fcd34d;\n}\nQPushButton#warningBtn:hover {\n    background-color: #fcd34d;\n    color: #78350f;\n}\nQPushButton#clearBtn {\n    background-color: transparent;\n    color: ',
    f'{_COLORS["text_white"]}',
    ';\n    border: none;\n}\nQPushButton#clearBtn:hover {\n    color: ',
    f'{_COLORS["accent"]}',
    ';\n    background-color: transparent;\n}\nQPushButton#disabledBtn {\n    background-color: #f1f5f9;\n    color: #475569;\n    border: 1px solid #e2e8f0;\n}\nQPushButton#disabledBtn:hover {\n    background-color: #f1f5f9;\n    color: #475569;\n}\nQPushButton#sendBtn {\n    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0ea5e9, stop:1 #0284c7);\n    color: #ffffff;\n    border: none;\n    border-radius: 10px;\n    padding: 10px 18px;\n    font-size: 16px;\n}\nQPushButton#sendBtn:hover {\n    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #38bdf8, stop:1 #0369a1);\n}\nQPushButton#sendBtn:disabled {\n    background-color: #cbd5e1;\n    color: #94a3b8;\n}\nQProgressBar {\n    background-color: ',
    f'{_COLORS["progress_bg"]}',
    ';\n    border-radius: 6px;\n    border: none;\n    height: 8px;\n    text-align: right;\n    color: transparent;\n}\nQProgressBar::chunk {\n    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #38bdf8, stop:1 #0284c7);\n    border-radius: 6px;\n}\nQFrame#rightPanel {\n    background-color: ',
    f'{_COLORS["bg_panel"]}',
    ';\n    border: 1px solid ',
    f'{_COLORS["border"]}',
    ';\n    border-radius: 16px;\n}\nQFrame#sidebarFrame {\n    background-color: ',
    f'{_COLORS["sidebar_bg"]}',
    ';\n    border-right: 1px solid ',
    f'{_COLORS["sidebar_border"]}',
    ';\n    border-radius: 0px;\n}\nQListWidget {\n    background-color: transparent;\n    border: none;\n    color: ',
    f'{_COLORS["text"]}',
    ';\n    font-size: 13px;\n}\nQListWidget::item {\n    padding: 8px 10px;\n    border-radius: 8px;\n}\nQListWidget::item:selected {\n    background-color: #e0f2fe;\n    color: ',
    f'{_COLORS["accent"]}',
    ';\n}\nQListWidget::item:hover {\n    background-color: #f1f5f9;\n}\nQComboBox {\n    background-color: ',
    f'{_COLORS["bg_input"]}',
    ';\n    border: 1px solid ',
    f'{_COLORS["border"]}',
    ';\n    border-radius: 8px;\n    padding: 4px 12px;\n    color: ',
    f'{_COLORS["text_white"]}',
    ';\n    font-size: 13px;\n}\nQComboBox::drop-down {\n    border: none;\n    width: 20px;\n}\nQComboBox QAbstractItemView {\n    background-color: #ffffff;\n    border: 1px solid ',
    f'{_COLORS["border"]}',
    ';\n    border-radius: 8px;\n    color: ',
    f'{_COLORS["text_white"]}',
    ';\n}\nQScrollArea {\n    border: none;\n    background: transparent;\n}\nQMessageBox {\n    background-color: ',
    f'{_COLORS["bg_card"]}',
    ';\n    color: ',
    f'{_COLORS["text_white"]}',
    ';\n}\nQMessageBox QLabel {\n    color: ',
    f'{_COLORS["text_white"]}',
    ';\n    background-color: transparent;\n}\nQMessageBox QPushButton {\n    background-color: ',
    f'{_COLORS["bg_input"]}',
    ';\n    color: ',
    f'{_COLORS["text_white"]}',
    ';\n    border: 1px solid ',
    f'{_COLORS["border"]}',
    ';\n    border-radius: 8px;\n    padding: 8px 20px;\n    min-width: 80px;\n}\nQMessageBox QPushButton:hover {\n    background-color: ',
    f'{_COLORS["accent"]}',
    ';\n    color: ',
    f'{_COLORS["accent_fg"]}',
    ';\n    border: 1px solid ',
    f'{_COLORS["accent"]}',
    ';\n}\nQMessageBox QPushButton:default {\n    background-color: ',
    f'{_COLORS["accent"]}',
    ';\n    color: ',
    f'{_COLORS["accent_fg"]}',
    ';\n    border: 1px solid ',
    f'{_COLORS["accent"]}',
    ';\n}\n',
])


class ToggleSwitch(QAbstractButton):
    """Animated iOS-style on/off toggle. Drop-in replacement for QCheckBox
    — exposes the same isChecked() / setChecked() / toggled signal API."""

    _W, _H = 40, 22
    _R = 11
    _KR = 9
    _PAD = 2
    _COLOR_OFF = QColor('#cbd5e1')
    _COLOR_ON = QColor(_COLORS['accent'])
    _COLOR_KNOB = QColor('#ffffff')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(self._W, self._H)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._knob_x = float(self._PAD)
        self._anim = QPropertyAnimation(self, b'knob_x', self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.toggled.connect(self._on_toggled)

    def _get_knob_x(self) -> float:
        return self._knob_x

    def _set_knob_x(self, value: float):
        self._knob_x = value
        self.update()

    knob_x = pyqtProperty(float, _get_knob_x, _set_knob_x)

    def _on_toggled(self, checked: bool):
        end = float(self._W - self._H + self._PAD) if checked else float(self._PAD)
        self._anim.stop()
        self._anim.setStartValue(self._knob_x)
        self._anim.setEndValue(end)
        self._anim.start()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = (self._knob_x - self._PAD) / max(1.0, self._W - self._H)
        t = max(0.0, min(1.0, t))
        r = int(self._COLOR_OFF.red() + t * (self._COLOR_ON.red() - self._COLOR_OFF.red()))
        g = int(self._COLOR_OFF.green() + t * (self._COLOR_ON.green() - self._COLOR_OFF.green()))
        b = int(self._COLOR_OFF.blue() + t * (self._COLOR_ON.blue() - self._COLOR_OFF.blue()))
        bg = QColor(r, g, b)
        p.setBrush(QBrush(bg))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, self._W, self._H, self._R, self._R)
        p.setBrush(QBrush(self._COLOR_KNOB))
        p.drawEllipse(int(self._knob_x), self._PAD, self._H - 2 * self._PAD, self._H - 2 * self._PAD)
        p.end()

    def sizeHint(self) -> QSize:
        return QSize(self._W, self._H)


def _make_paperclip_icon(size: int = 20, color: str = _COLORS['text_dim']) -> QIcon:
    """Draw a paperclip icon via QPainter (no external deps)."""
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.translate(size / 2, size / 2)
    p.rotate(-40)
    s = size / 22
    outer = QRectF(-4.5 * s, -9.5 * s, 9 * s, 19 * s)
    p.drawRoundedRect(outer, 4.5 * s, 4.5 * s)
    path = QPainterPath()
    path.moveTo(-2.2 * s, 3.5 * s)
    path.lineTo(-2.2 * s, -5.8 * s)
    path.arcTo(QRectF(-2.2 * s, -9 * s, 4.4 * s, 6.4 * s), 180, -180)
    path.lineTo(2.2 * s, 3.5 * s)
    p.drawPath(path)
    p.end()
    return QIcon(px)


class ChatInputBox(QTextEdit):
    """Multi-line input with automatic height and Enter/Shift+Enter."""

    send_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(56)
        self.setPlaceholderText(T('ui.type_message'))
        # fmt: off
        self.setStyleSheet(
            f'\n            QTextEdit {{\n                background-color: #ffffff;\n                border: 2px solid {_COLORS["border"]};\n                border-radius: 12px;\n                padding: 12px 16px;\n                font-size: 14px;\n                color: {_COLORS["text"]};\n            }}\n            QTextEdit:focus {{\n                border: 2px solid {_COLORS["accent"]};\n            }}\n        '
        )
        # fmt: on
        self.document().contentsChanged.connect(self._adjust_height)

    def _adjust_height(self):
        doc_height = int(self.document().size().height()) + 28
        self.setFixedHeight(max(56, min(doc_height, 160)))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                text = self.toPlainText().strip()
                self.send_requested.emit(text)
                self.clear()
                self.setFixedHeight(56)
                return
        super().keyPressEvent(event)


def _normalize_projection_result(result):
    """Validate and align a successful analysis projection response.

    Version 2 keeps positional arrays for drawing while also exposing stable
    record identities. Missing optional arrays receive safe defaults;
    explicitly supplied arrays must remain aligned with ``n_points``.
    """
    if not isinstance(result, dict) or result.get('status') != 'success':
        raise ValueError('projection response is not successful')

    version = result.get('response_version', 1)
    if isinstance(version, bool):
        raise ValueError('invalid response_version')
    try:
        version = int(version)
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid response_version') from exc
    if version not in (1, 2):
        raise ValueError(f'unsupported response_version: {version}')

    n_points = result.get('n_points')
    if isinstance(n_points, bool):
        raise ValueError('invalid n_points')
    try:
        n_points = int(n_points)
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid n_points') from exc
    if n_points < 0:
        raise ValueError('invalid n_points')

    memory_type = result.get('memory_type')
    if memory_type is not None:
        memory_type = str(memory_type).lower()
        if memory_type not in ('stm', 'ltm'):
            raise ValueError('invalid memory_type')

    def _aligned(name, default_factory):
        value = result.get(name)
        if value is None:
            return [default_factory(i) for i in range(n_points)]
        if not isinstance(value, (list, tuple)) or len(value) != n_points:
            raise ValueError(f'{name} must contain exactly n_points items')
        return list(value)

    normalized = dict(result)
    normalized.update({
        'response_version': version,
        'memory_type': memory_type,
        'n_points': n_points,
        'indices': _aligned('indices', lambda i: i),
        'key_texts': _aligned('key_texts', lambda _i: ''),
        'value_texts': _aligned('value_texts', lambda _i: ''),
        'memory_ids': _aligned('memory_ids', lambda _i: None),
        'provenances': _aligned('provenances', lambda _i: {}),
        'intensities': _aligned('intensities', lambda _i: 1),
        'usages': _aligned('usages', lambda _i: 1),
        'ages': _aligned('ages', lambda _i: 0),
        'nodes': _aligned('nodes', lambda _i: {}),
    })

    if any(isinstance(index, bool) or not isinstance(index, int)
           for index in normalized['indices']):
        raise ValueError('indices must contain integers')

    linkage = result.get('linkage_matrix', result.get('linkage', []))
    if not isinstance(linkage, (list, tuple)):
        raise ValueError('linkage_matrix must be a sequence')
    normalized['linkage_matrix'] = list(linkage)

    raw_edges = result.get('edges', [])
    if not isinstance(raw_edges, (list, tuple)):
        raise ValueError('edges must be a sequence')
    edges = []
    for edge in raw_edges:
        if not isinstance(edge, (list, tuple)) or len(edge) < 3:
            raise ValueError('each edge must be a source/target/weight triple')
        source, target, weight = edge[:3]
        if (isinstance(source, bool) or not isinstance(source, int)
                or isinstance(target, bool) or not isinstance(target, int)
                or source < 0 or target < 0
                or source >= n_points or target >= n_points):
            raise ValueError('edge endpoints must be local node positions')
        try:
            weight = float(weight)
        except (TypeError, ValueError) as exc:
            raise ValueError('edge weight must be numeric') from exc
        edges.append([source, target, weight])
    normalized['edges'] = edges

    raw_edge_records = result.get('edge_records', [])
    if not isinstance(raw_edge_records, (list, tuple)):
        raise ValueError('edge_records must be a sequence')
    edge_records = []
    for edge in raw_edge_records:
        if not isinstance(edge, dict) or 'source' not in edge or 'target' not in edge or 'weight' not in edge:
            raise ValueError('each edge_record must contain source, target, and weight')
        try:
            weight = float(edge['weight'])
        except (TypeError, ValueError) as exc:
            raise ValueError('edge_record weight must be numeric') from exc
        stable_edge = dict(edge)
        stable_edge['weight'] = weight
        edge_records.append(stable_edge)
    normalized['edge_records'] = edge_records
    return normalized


class DendrogramWidget(QWidget):
    """
    Draws a dendrogram from the linkage matrix using QPainter.

    No external dependencies on matplotlib or D3.js.
    Main branches are color-distinguished (HSL by index modulo 360).
    A leaf is highlighted on hover and shows a tooltip.
    """

    _MARGIN_LEFT = 60
    _MARGIN_RIGHT = 20
    _MARGIN_TOP = 20
    _MARGIN_BOTTOM = 40
    _CLUSTER_COLOR_THRESHOLD = 0.7

    leaf_clicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._linkage = []
        self._n_points = 0
        self._intensities = []
        self._usages = []
        self._thickness_mode = 'uniform'
        self._color_threshold = 0.4
        self._leaf_rects = []
        self._branch_rects = []
        self._hovered_leaf = -1
        self._hovered_cluster = -1
        self._zoom = 1
        self._pan_x = 0
        self._drag_active = False
        self._drag_start_x = 0
        self._drag_pan_start = 0
        self.setMouseTracking(True)
        self.setMinimumSize(400, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_data(self, linkage_matrix: list, n_points: int, intensities: list = None, usages: list = None):
        """Sets the data and redraws the widget."""
        self._linkage = linkage_matrix
        self._n_points = n_points
        self._intensities = intensities or [1] * n_points
        self._usages = usages or [1] * n_points
        self._leaf_rects = []
        self._branch_rects = []
        self._hovered_leaf = -1
        self._hovered_cluster = -1
        self._zoom = 1
        self._pan_x = 0
        self.update()

    def set_thickness_mode(self, mode: str):
        """Sets the line thickness mode ('uniform', 'intensity', 'frequency', 'combined')."""
        if self._thickness_mode != mode:
            self._thickness_mode = mode
            self.update()

    def set_color_threshold(self, ratio: float):
        """Sets the dynamic color threshold for clusters (0.05 - 0.98)."""
        if self._color_threshold != ratio:
            self._color_threshold = max(0.05, min(0.98, ratio))
            self.update()

    def reset_view(self):
        self._zoom = 1
        self._pan_x = 0
        self.update()

    def _clamp_pan(self):
        """Keeps _pan_x within the valid range."""
        ml = self._MARGIN_LEFT
        mr = self._MARGIN_RIGHT
        canvas_w = max(1, self.width() - ml - mr)
        max_pan = canvas_w * (self._zoom - 1)
        self._pan_x = max(0, min(self._pan_x, max_pan))

    def wheelEvent(self, event):
        """Zoom with the mouse wheel (horizontal stretch)."""
        if not self._linkage or self._n_points < 2:
            event.ignore()
            return
        ml = self._MARGIN_LEFT
        pos = event.position() if hasattr(event, 'position') else event.pos()
        mx = pos.x()
        delta = event.angleDelta().y()
        factor = 1.15 if delta > 0 else 0.869565
        old_zoom = self._zoom
        new_zoom = max(1, min(500, old_zoom * factor))
        if new_zoom == old_zoom:
            return
        vx_cursor = mx + self._pan_x - ml
        scale = new_zoom / old_zoom
        self._pan_x = ml + vx_cursor * scale - mx
        if self._pan_x < 0:
            self._pan_x = 0
        self._zoom = new_zoom
        self._clamp_pan()
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        pos = event.position() if hasattr(event, 'position') else event.pos()
        px, py = pos.x(), pos.y()
        btn = event.button()
        if btn == Qt.MouseButton.LeftButton:
            for i, (rect, cluster_id) in enumerate(self._leaf_rects):
                if rect.contains(px, py):
                    self.leaf_clicked.emit(int(cluster_id))
                    return
        elif btn in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._drag_active = True
            self._drag_start_x = px
            self._drag_pan_start = self._pan_x
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseReleaseEvent(self, event):
        if self._drag_active:
            self._drag_active = False
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseMoveEvent(self, event):
        pos = event.position() if hasattr(event, 'position') else event.pos()
        px, py = pos.x(), pos.y()
        if self._drag_active:
            delta = px - self._drag_start_x
            self._pan_x = self._drag_pan_start - delta
            self._clamp_pan()
            self.update()
            return
        found_leaf = -1
        for i, (rect, cluster_id) in enumerate(self._leaf_rects):
            if rect.contains(px, py):
                found_leaf = i
                break
        found_cluster = -1
        if found_leaf >= 0:
            found_cluster = self._leaf_rects[found_leaf][1]
        else:
            for rect, merge_id, _ in self._branch_rects:
                if rect.contains(px, py):
                    found_cluster = merge_id
                    break
        if found_leaf != self._hovered_leaf or found_cluster != self._hovered_cluster:
            self._hovered_leaf = found_leaf
            self._hovered_cluster = found_cluster
            self.update()
            if found_leaf >= 0:
                _, cluster_id = self._leaf_rects[found_leaf]
                tip_text = f'LTM centre #{cluster_id}'
                if cluster_id < len(self._intensities):
                    tip_text += f'\n{T("memory.center_intensity")}: {self._intensities[cluster_id]:.4f}'
                if cluster_id < len(self._usages):
                    tip_text += f'\n{T("memory.dendrogram_usage")}: {self._usages[cluster_id]}'
                QToolTip.showText(
                    event.globalPosition().toPoint() if hasattr(event, 'globalPosition') else event.globalPos(),
                    tip_text,
                    self,
                )
            elif found_cluster >= self._n_points:
                tip_text = f'LTM Cluster #{found_cluster - self._n_points + 1}'
                QToolTip.showText(
                    event.globalPosition().toPoint() if hasattr(event, 'globalPosition') else event.globalPos(),
                    tip_text,
                    self,
                )
            else:
                QToolTip.hideText()
        on_item = found_leaf >= 0 or found_cluster >= 0
        self.setCursor(Qt.CursorShape.PointingHandCursor if on_item else Qt.CursorShape.ArrowCursor)

    def leaveEvent(self, event):
        self._hovered_leaf = -1
        self._hovered_cluster = -1
        self._drag_active = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        ml, mr, mt, mb = self._MARGIN_LEFT, self._MARGIN_RIGHT, self._MARGIN_TOP, self._MARGIN_BOTTOM
        canvas_w = w - ml - mr
        canvas_h = h - mt - mb
        p.fillRect(0, 0, w, h, QColor('#f8fafc'))
        if not self._linkage or self._n_points < 2:
            p.setPen(QColor('#334155'))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, T('memory.dendrogram_empty'))
            p.end()
            return

        n = self._n_points
        Z = self._linkage
        n_m = len(Z)
        virtual_w = canvas_w * self._zoom
        pan = self._pan_x
        leaf_x = {}
        leaf_order = self._compute_leaf_order(Z, n)
        for rank, cid in enumerate(leaf_order):
            leaf_x[cid] = ml + (rank + 0.5) * (virtual_w / n) - pan

        cluster_x = {}
        for cid in range(n):
            cluster_x[cid] = leaf_x[cid]

        max_dist = max(row[2] for row in Z) if Z else 1
        if max_dist == 0:
            max_dist = 1
        threshold = max_dist * self._color_threshold
        self._leaf_rects = []
        self._branch_rects = []

        def dist_to_y(d: float) -> float:
            return mt + canvas_h * (1 - d / max_dist)

        VIBRANT_PALETTE = [
            QColor('#2563eb'), QColor('#10b981'), QColor('#e11d48'), QColor('#f59e0b'),
            QColor('#8b5cf6'), QColor('#06b6d4'), QColor('#ec4899'), QColor('#4f46e5'),
            QColor('#84cc16'), QColor('#f97316'), QColor('#14b8a6'), QColor('#d946ef'),
            QColor('#0284c7'), QColor('#16a34a'), QColor('#dc2626'), QColor('#eab308'),
            QColor('#7c3aed'), QColor('#0891b2'), QColor('#db2777'), QColor('#6366f1'),
            QColor('#ea580c'), QColor('#059669'), QColor('#c026d3'), QColor('#3b82f6'),
        ]
        top_branch_color = QColor('#475569')
        parent_map = {}
        children_map = {}
        leaf_counts = {cid: 1 for cid in range(n)}
        metric_values = {}

        for cid in range(n):
            if self._thickness_mode == 'intensity':
                val = float(self._intensities[cid]) if cid < len(self._intensities) else 1
            elif self._thickness_mode == 'frequency':
                val = float(self._usages[cid]) if cid < len(self._usages) else 1
            elif self._thickness_mode == 'combined':
                h_val = float(self._intensities[cid]) if cid < len(self._intensities) else 1
                u_val = float(self._usages[cid]) if cid < len(self._usages) else 1
                val = h_val * (1 + math.log1p(u_val))
            else:
                val = 1
            metric_values[cid] = val

        for i, row in enumerate(Z):
            left_id = int(row[0])
            right_id = int(row[1])
            merge_id = n + i
            parent_map[left_id] = merge_id
            parent_map[right_id] = merge_id
            children_map[merge_id] = [left_id, right_id]
            cnt_l = leaf_counts.get(left_id, 1)
            cnt_r = leaf_counts.get(right_id, 1)
            cnt_m = int(row[3]) if len(row) > 3 else cnt_l + cnt_r
            leaf_counts[merge_id] = cnt_m
            metric_values[merge_id] = (
                metric_values.get(left_id, 1) * cnt_l
                + metric_values.get(right_id, 1) * cnt_r
            ) / max(1, cnt_m)

        active_nodes = set()
        if self._hovered_cluster >= 0:
            curr = self._hovered_cluster
            while curr in parent_map:
                active_nodes.add(curr)
                curr = parent_map[curr]
            active_nodes.add(curr)
            stack = [self._hovered_cluster]
            while stack:
                u = stack.pop()
                active_nodes.add(u)
                stack.extend(children_map.get(u, []))

        node_color = {}
        cluster_idx = 0
        for i in range(n_m - 1, -1, -1):
            left_id = int(Z[i][0])
            right_id = int(Z[i][1])
            dist = Z[i][2]
            merge_id = n + i
            if dist > threshold:
                node_color[merge_id] = top_branch_color
                for cid, c_dist in (
                    (left_id, Z[left_id - n][2] if left_id >= n else 0),
                    (right_id, Z[right_id - n][2] if right_id >= n else 0),
                ):
                    if c_dist <= threshold and cid not in node_color:
                        if cluster_idx < len(VIBRANT_PALETTE):
                            node_color[cid] = VIBRANT_PALETTE[cluster_idx]
                        else:
                            node_color[cid] = QColor.fromHsl(int(cluster_idx * 137.5) % 360, 200, 130)
                        cluster_idx += 1
            else:
                if merge_id not in node_color:
                    if cluster_idx < len(VIBRANT_PALETTE):
                        node_color[merge_id] = VIBRANT_PALETTE[cluster_idx]
                    else:
                        node_color[merge_id] = QColor.fromHsl(int(cluster_idx * 137.5) % 360, 200, 130)
                    cluster_idx += 1
                color = node_color[merge_id]
                if left_id not in node_color:
                    node_color[left_id] = color
                if right_id not in node_color:
                    node_color[right_id] = color

        for cid in range(n + n_m):
            if cid not in node_color:
                if cluster_idx < len(VIBRANT_PALETTE):
                    node_color[cid] = VIBRANT_PALETTE[cluster_idx]
                else:
                    node_color[cid] = QColor.fromHsl(int(cluster_idx * 137.5) % 360, 200, 130)
                cluster_idx += 1

        all_vals = list(metric_values.values())
        min_val = min(all_vals) if all_vals else 0
        max_val = max(all_vals) if all_vals else 1
        val_range = max_val - min_val
        MIN_WIDTH = 1.6
        MAX_WIDTH = 5.5

        def get_line_width(node_id: int) -> float:
            if self._thickness_mode == 'uniform' or val_range < 1e-09:
                base = 2
            else:
                val = metric_values.get(node_id, min_val)
                t = max(0, min(1, (val - min_val) / val_range))
                if self._thickness_mode in ('frequency', 'combined'):
                    t = math.pow(t, 0.6)
                base = MIN_WIDTH + t * (MAX_WIDTH - MIN_WIDTH)
            if active_nodes and node_id in active_nodes:
                return base + 1.5
            return base

        def get_node_pen_color(node_id: int, base_col: QColor) -> QColor:
            if not active_nodes:
                return base_col
            if node_id in active_nodes:
                return base_col
            c = QColor(base_col)
            c.setAlpha(45)
            return c

        p.setClipRect(QRectF(ml, mt, canvas_w, canvas_h + self._MARGIN_BOTTOM))
        p.setClipping(False)
        p.setPen(QPen(QColor('#e2e8f0'), 1))
        p.setFont(QFont('Segoe UI', 8))
        n_ticks = 5
        for i in range(n_ticks + 1):
            d_val = max_dist * i / n_ticks
            y_val = dist_to_y(d_val)
            p.setPen(QPen(QColor('#e2e8f0'), 1, Qt.PenStyle.DashLine))
            p.drawLine(int(ml), int(y_val), int(w - mr), int(y_val))
            p.setPen(QColor('#334155'))
            p.drawText(
                QRectF(0, y_val - 8, ml - 4, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f'{d_val:.2f}',
            )
        p.setClipping(True)
        leaf_y = mt + canvas_h
        r = max(3.5, min(6, virtual_w / n / 3.5))
        for rank, cid in enumerate(leaf_order):
            lx = leaf_x[cid]
            rect = QRectF(lx - r, leaf_y - r, 2 * r, 2 * r)
            self._leaf_rects.append((rect, cid))
            if lx + r < ml or lx - r > w - mr:
                continue
            if self._hovered_leaf == len(self._leaf_rects) - 1 or (active_nodes and cid in active_nodes):
                col = (
                    QColor('#0ea5e9')
                    if self._hovered_leaf == len(self._leaf_rects) - 1
                    else get_node_pen_color(cid, node_color.get(cid, top_branch_color))
                )
                p.setBrush(col)
                p.setPen(
                    QPen(QColor('#ffffff'), 1.5 if (active_nodes and cid in active_nodes) else 1)
                )
            else:
                leaf_col = get_node_pen_color(cid, node_color.get(cid, top_branch_color))
                p.setBrush(leaf_col)
                p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(rect)

        for i, row in enumerate(Z):
            left_id = int(row[0])
            right_id = int(row[1])
            dist = row[2]
            merge_id = n + i
            lx = cluster_x.get(left_id, ml)
            rx = cluster_x.get(right_id, ml)
            cy = dist_to_y(dist)
            right_edge = w - mr
            if max(lx, rx) < ml or min(lx, rx) > right_edge:
                cluster_x[merge_id] = (lx + rx) / 2
                continue
            left_y = dist_to_y(Z[left_id - n][2]) if left_id >= n else leaf_y
            right_y = dist_to_y(Z[right_id - n][2]) if right_id >= n else leaf_y
            base_col = top_branch_color if dist > threshold else node_color.get(merge_id, top_branch_color)
            col_left = get_node_pen_color(left_id, node_color.get(left_id, base_col))
            col_right = get_node_pen_color(right_id, node_color.get(right_id, base_col))
            col_merge = get_node_pen_color(merge_id, base_col)
            p.setBrush(Qt.BrushStyle.NoBrush)
            pen_left = QPen(col_left, get_line_width(left_id))
            pen_left.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen_left)
            p.drawLine(int(lx), int(left_y), int(lx), int(cy))
            self._branch_rects.append(
                (QRectF(lx - 5, min(left_y, cy) - 2, 10, abs(cy - left_y) + 4), left_id, dist)
            )
            pen_right = QPen(col_right, get_line_width(right_id))
            pen_right.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen_right)
            p.drawLine(int(rx), int(right_y), int(rx), int(cy))
            self._branch_rects.append(
                (QRectF(rx - 5, min(right_y, cy) - 2, 10, abs(cy - right_y) + 4), right_id, dist)
            )
            pen_merge = QPen(col_merge, get_line_width(merge_id))
            pen_merge.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen_merge)
            p.drawLine(int(lx), int(cy), int(rx), int(cy))
            self._branch_rects.append(
                (QRectF(min(lx, rx) - 2, cy - 5, abs(rx - lx) + 4, 10), merge_id, dist)
            )
            cluster_x[merge_id] = (lx + rx) / 2

        p.setClipping(False)
        if self._zoom > 1.01:
            p.setFont(QFont('Segoe UI', 9))
            p.setPen(QColor('#334155'))
            zoom_txt = f'zoom {self._zoom:.1f}x  (right-click + drag = pan)'
            p.drawText(QRectF(ml, mt + 2, canvas_w, 16), Qt.AlignmentFlag.AlignRight, zoom_txt)
        p.end()

    @staticmethod
    def _compute_leaf_order(Z: list, n: int) -> list:
        """Returns the leaf order for drawing the dendrogram (iterative DFS, no recursion limit)."""
        children = {}
        for i, row in enumerate(Z):
            children[n + i] = (int(row[0]), int(row[1]))
        order = []
        stack = [n + len(Z) - 1]
        while stack:
            node = stack.pop()
            if node < n:
                order.append(node)
            else:
                left, right = children[node]
                stack.append(right)
                stack.append(left)
        return order


class RefactorProgressDialog(QDialog):
    """
    Modal dialog with a progress bar for rebuilding the cognitive terrain.
    Shows the progress of re-writing records and the result of the operation.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(T('memory.refactor_title'))
        self.setMinimumSize(480, 260)
        self.resize(520, 280)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowCloseButtonHint)
        self.setModal(True)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        title = QLabel(T('memory.refactor_title'))
        title.setFont(QFont('Segoe UI', 14, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {_COLORS['text']};")
        layout.addWidget(title)
        self.step_label = QLabel(T('memory.refactor_step_backup'))
        self.step_label.setStyleSheet(f"color: {_COLORS['text_dim']}; font-size: 13px;")
        self.step_label.setWordWrap(True)
        layout.addWidget(self.step_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(0)
        self.progress_bar.setFixedHeight(12)
        layout.addWidget(self.progress_bar)
        self.detail_label = QLabel('')
        self.detail_label.setStyleSheet(f"color: {_COLORS['text_dim']}; font-size: 11px; font-style: italic;")
        self.detail_label.setWordWrap(True)
        self.detail_label.setMaximumHeight(40)
        layout.addWidget(self.detail_label)
        layout.addStretch()
        self.close_btn = QPushButton(T('ui.close'))
        self.close_btn.setMinimumHeight(36)
        self.close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        layout.addWidget(self.close_btn)

    def update_progress(self, step: str, current: int, total: int, detail: str):
        """Updates the progress bar and labels from the main thread."""
        step_keys = {
            'backup': 'memory.refactor_step_backup',
            'collect': 'memory.refactor_step_collect',
            'reset': 'memory.refactor_step_reset',
            'replay': 'memory.refactor_step_replay',
            'verify': 'memory.refactor_step_verify',
        }
        step_key = step_keys.get(step, 'memory.refactor_step_replay')
        if step == 'replay' and total > 0:
            self.step_label.setText(T('memory.refactor_progress').format(current, total))
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)
        else:
            self.step_label.setText(T(step_key))
            if step in ('backup', 'collect', 'reset', 'verify'):
                self.progress_bar.setMaximum(0)
        if detail:
            self.detail_label.setText(detail)

    def closeEvent(self, event):
        if not self.close_btn.isEnabled():
            event.ignore()
            return
        super().closeEvent(event)

    def show_result(self, result: dict):
        """Shows the result of the cognitive terrain rebuild."""
        self.progress_bar.setMaximum(1)
        self.progress_bar.setValue(1)
        self.close_btn.setEnabled(True)
        if result.get('status') == 'success':
            msg = T('memory.refactor_success').format(
                result.get('records_replayed', 0),
                result.get('stm_active', 0),
                result.get('ltm_active', 0),
                result.get('verification_rate', 0),
            )
            self.step_label.setText(msg)
            self.step_label.setStyleSheet(f"color: {_COLORS['success']}; font-size: 13px;")
        else:
            msg = T('memory.refactor_failed').format(result.get('error', '?'))
            self.step_label.setText(msg)
            self.step_label.setStyleSheet(f"color: {_COLORS['error']}; font-size: 13px;")
        self.detail_label.setText('')


class CenterDetailDialog(QDialog):
    """
    Dialog showing the details of a single LTM center.
    Allows viewing, editing and deleting the stored texts.
    """

    _load_ready = pyqtSignal(dict)
    _action_ready = pyqtSignal(dict)

    def __init__(self, parent, center_idx: int, command_handler, async_loop, memory_type: str = 'ltm'):
        super().__init__(parent)
        self._center_idx = center_idx
        self._command_handler = command_handler
        self._async_loop = async_loop
        self._memory_type = memory_type.lower()
        self.center_deleted = False
        self.center_updated = False
        self._load_ready.connect(self._on_loaded)
        self._action_ready.connect(self._on_action_done)
        title_str = T('memory.center_title').format(center_idx).replace('LTM', self._memory_type.upper())
        self.setWindowTitle(title_str)
        self.setMinimumSize(560, 440)
        self.resize(620, 480)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowCloseButtonHint)
        self._build_ui()
        QTimer.singleShot(0, self._load_data)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        title_str = T('memory.center_title').format(self._center_idx).replace('LTM', self._memory_type.upper())
        title = QLabel(title_str)
        title.setFont(QFont('Segoe UI', 13, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {_COLORS['text']};")
        layout.addWidget(title)
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet('background-color: #e2e8f0;')
        layout.addWidget(sep)
        stats_row = QHBoxLayout()
        self._stat_h = QLabel('')
        self._stat_h.setStyleSheet(f"color: {_COLORS['text_dim']}; font-size: 11px;")
        self._stat_u = QLabel('')
        self._stat_u.setStyleSheet(f"color: {_COLORS['text_dim']}; font-size: 11px;")
        stats_row.addWidget(self._stat_h)
        stats_row.addSpacing(20)
        stats_row.addWidget(self._stat_u)
        stats_row.addStretch()
        layout.addLayout(stats_row)
        key_lbl = QLabel(T('memory.center_key'))
        key_lbl.setFont(QFont('Segoe UI', 10, QFont.Weight.DemiBold))
        key_lbl.setStyleSheet(f"color: {_COLORS['text']};")
        layout.addWidget(key_lbl)
        self._key_edit = QTextEdit()
        self._key_edit.setFixedHeight(80)
        self._key_edit.setStyleSheet(
            f"background: {_COLORS['bg_input']}; border: 1px solid {_COLORS['border']}; "
            f"border-radius: 6px; padding: 6px; font-size: 12px; color: {_COLORS['text']};"
        )
        layout.addWidget(self._key_edit)
        val_lbl = QLabel(T('memory.center_value'))
        val_lbl.setFont(QFont('Segoe UI', 10, QFont.Weight.DemiBold))
        val_lbl.setStyleSheet(f"color: {_COLORS['text']};")
        layout.addWidget(val_lbl)
        self._val_edit = QTextEdit()
        self._val_edit.setStyleSheet(
            f"background: {_COLORS['bg_input']}; border: 1px solid {_COLORS['border']}; "
            f"border-radius: 6px; padding: 6px; font-size: 12px; color: {_COLORS['text']};"
        )
        layout.addWidget(self._val_edit, 1)
        self._status_lbl = QLabel(T('memory.center_loading'))
        self._status_lbl.setStyleSheet(f"color: {_COLORS['text_dim']}; font-size: 11px;")
        layout.addWidget(self._status_lbl)
        refactor_hint_lbl = QLabel(T('memory.center_refactor_hint'))
        refactor_hint_lbl.setWordWrap(True)
        refactor_hint_lbl.setStyleSheet(f"color: {_COLORS['warning']}; font-size: 11px; font-weight: 500;")
        layout.addWidget(refactor_hint_lbl)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._save_btn = QPushButton(T('memory.center_save'))
        self._save_btn.setEnabled(False)
        self._save_btn.setFixedHeight(34)
        self._save_btn.setStyleSheet(
            f"QPushButton {{ background: {_COLORS['accent']}; color: white; border: none; "
            f"border-radius: 6px; padding: 0 16px; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {_COLORS['accent_hover']}; }}"
            f"QPushButton:disabled {{ background: {_COLORS['border']}; color: {_COLORS['text_dim']}; }}"
        )
        self._save_btn.clicked.connect(self._save_data)
        self._del_btn = QPushButton(T('memory.center_delete'))
        self._del_btn.setEnabled(False)
        self._del_btn.setFixedHeight(34)
        self._del_btn.setStyleSheet(
            f"QPushButton {{ background: #fff0f0; color: #dc2626; border: 1px solid #fca5a5; "
            f"border-radius: 6px; padding: 0 16px; font-size: 12px; }}"
            f"QPushButton:hover {{ background: #dc2626; color: white; }}"
            f"QPushButton:disabled {{ background: {_COLORS['border']}; color: {_COLORS['text_dim']}; border: none; }}"
        )
        self._del_btn.clicked.connect(self._delete_center)
        _close_labels = {
            'en': 'Close',
            'cz': 'Zavrit',
            'de': 'Schliessen',
            'fr': 'Fermer',
            'pl': 'Zamknij',
        }
        from .localization import Localization as _Loc
        _close_lbl = _close_labels.get(_Loc._lang, 'Close')
        close_btn = QPushButton(_close_lbl)
        close_btn.setFixedHeight(34)
        close_btn.setStyleSheet(
            f"QPushButton {{ background: {_COLORS['bg_input']}; border: 1px solid {_COLORS['border']}; "
            f"border-radius: 6px; padding: 0 14px; font-size: 12px; color: {_COLORS['text']}; }}"
            f"QPushButton:hover {{ background: {_COLORS['border']}; }}"
        )
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._save_btn)
        btn_row.addWidget(self._del_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _load_data(self):
        """Asynchronously loads the center data from the backend."""

        async def _run():
            result = await self._command_handler.handle(
                command='get_center',
                index=self._center_idx,
                memory_type=self._memory_type,
            )
            self._load_ready.emit(result)

        self._async_loop.call_soon_threadsafe(asyncio.ensure_future(_run()))

    def _on_loaded(self, result: dict):
        """Fills the UI with data after loading."""
        if result.get('status') != 'success':
            self._status_lbl.setText(T('memory.center_error').format(result.get('error', '?')))
            return
        key_text = result.get('key_text', '')
        value_text = result.get('value_text', '')
        h = result.get('h', 0)
        usage = result.get('usage', 0)
        self._key_edit.setPlainText(key_text if key_text else T('memory.center_empty'))
        self._val_edit.setPlainText(value_text if value_text else T('memory.center_empty'))
        self._stat_h.setText(f"{T('memory.center_intensity')}: {h:.4f}")
        self._stat_u.setText(f"{T('memory.center_usage')}: {usage}")
        self._status_lbl.setText('')
        self._save_btn.setEnabled(True)
        self._del_btn.setEnabled(True)

    def _save_data(self):
        """Saves the edited texts."""
        self._save_btn.setEnabled(False)
        self._del_btn.setEnabled(False)
        self._status_lbl.setText('...')
        key_text = self._key_edit.toPlainText().strip()
        value_text = self._val_edit.toPlainText().strip()

        async def _run():
            result = await self._command_handler.handle(
                command='update_center',
                index=self._center_idx,
                key_text=key_text,
                value_text=value_text,
                memory_type=self._memory_type,
            )
            self._action_ready.emit(result)

        self._async_loop.call_soon_threadsafe(asyncio.ensure_future(_run()))

    def _delete_center(self):
        """Deletes the center after confirmation."""
        confirm_msg = T('memory.center_confirm_delete').format(self._center_idx).replace(
            'LTM', self._memory_type.upper())
        reply = QMessageBox.question(
            self,
            T('memory.center_delete'),
            confirm_msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._save_btn.setEnabled(False)
        self._del_btn.setEnabled(False)
        self._status_lbl.setText('...')

        async def _run():
            result = await self._command_handler.handle(
                command='delete_center',
                index=self._center_idx,
                memory_type=self._memory_type,
            )
            self._action_ready.emit(result)

        self._async_loop.call_soon_threadsafe(asyncio.ensure_future(_run()))

    def _on_action_done(self, result: dict):
        """Processes the result of a save/delete action."""
        cmd = result.get('command', '')
        if result.get('status') != 'success':
            self._status_lbl.setText(T('memory.center_error').format(result.get('error', '?')))
            self._save_btn.setEnabled(True)
            self._del_btn.setEnabled(True)
            return
        if cmd == 'update_center':
            self._status_lbl.setText(T('memory.center_saved'))
            self._save_btn.setEnabled(True)
            self._del_btn.setEnabled(True)
            self.center_updated = True
            return
        if cmd == 'delete_center':
            self._status_lbl.setText(T('memory.center_deleted'))
            self.center_deleted = True
            QTimer.singleShot(800, self.accept)
            return


class DendrogramWindow(QDialog):
    """
    Standalone window showing the dendrogram of the biomem memory.
    The caller passes a command_handler and async_loop to load the data.
    """

    _result_ready = pyqtSignal(dict)

    def __init__(self, parent, command_handler, async_loop):
        super().__init__(parent)
        self._command_handler = command_handler
        self._async_loop = async_loop
        self._current_memory_type = 'ltm'
        self._indices = []
        self._result_ready.connect(self._on_result)
        self.setWindowTitle(T('memory.dendrogram_title'))
        self.setMinimumSize(700, 500)
        self.resize(860, 560)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowCloseButtonHint | Qt.WindowType.WindowMaximizeButtonHint)
        self._build_ui()
        self._chart.leaf_clicked.connect(self._on_leaf_clicked)
        QTimer.singleShot(0, self._fetch_dendrogram)

    def _on_leaf_clicked(self, center_idx: int):
        """Opens a center detail dialog."""
        if hasattr(self, '_indices') and center_idx < len(self._indices):
            real_idx = self._indices[center_idx]
        else:
            real_idx = center_idx
        dlg = CenterDetailDialog(self, real_idx, self._command_handler, self._async_loop,
                                 memory_type=self._current_memory_type)
        dlg.exec()
        if dlg.center_deleted or getattr(dlg, 'center_updated', False):
            self._fetch_dendrogram()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title_lbl = QLabel(T('memory.dendrogram_title'))
        title_lbl.setFont(QFont('Segoe UI', 14, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f'color: {_COLORS["text"]};')
        self._centers_lbl = QLabel('')
        self._centers_lbl.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px;')
        header.addWidget(title_lbl)
        header.addStretch()
        header.addWidget(self._centers_lbl)
        layout.addLayout(header)
        sub_layout = QHBoxLayout()
        self._subtitle = QLabel(T('memory.dendrogram_subtitle').replace('LTM', 'STM'))
        self._subtitle.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px;')
        sub_layout.addWidget(self._subtitle)
        sub_layout.addStretch()
        self._source_lbl = QLabel(T('memory.select_source'))
        self._source_lbl.setStyleSheet(f'color: {_COLORS["text"]}; font-size: 12px; font-weight: 600;')
        sub_layout.addWidget(self._source_lbl)
        self._source_combo = QComboBox()
        self._source_combo.addItem('STM', 'stm')
        self._source_combo.addItem('LTM', 'ltm')
        self._source_combo.setCurrentIndex(self._source_combo.findData(self._current_memory_type))
        self._source_combo.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._source_combo.setFixedHeight(32)
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)
        sub_layout.addWidget(self._source_combo)
        sub_layout.addSpacing(14)
        self._thick_lbl = QLabel(T('memory.dendrogram_thick_label'))
        self._thick_lbl.setStyleSheet(f'color: {_COLORS["text"]}; font-size: 12px; font-weight: 600;')
        sub_layout.addWidget(self._thick_lbl)
        self._thickness_combo = QComboBox()
        self._thickness_combo.addItem(T('memory.dendrogram_thick_uniform'), 'uniform')
        self._thickness_combo.addItem(T('memory.dendrogram_thick_intensity'), 'intensity')
        self._thickness_combo.addItem(T('memory.dendrogram_thick_frequency'), 'frequency')
        self._thickness_combo.addItem(T('memory.dendrogram_thick_combined'), 'combined')
        self._thickness_combo.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._thickness_combo.setFixedHeight(32)
        self._thickness_combo.currentIndexChanged.connect(self._on_thickness_mode_changed)
        sub_layout.addWidget(self._thickness_combo)
        sub_layout.addSpacing(14)
        self._thresh_lbl = QLabel(T('memory.dendrogram_color_thresh'))
        self._thresh_lbl.setStyleSheet(f'color: {_COLORS["text"]}; font-size: 12px; font-weight: 600;')
        sub_layout.addWidget(self._thresh_lbl)
        self._thresh_slider = QSlider(Qt.Orientation.Horizontal)
        self._thresh_slider.setRange(15, 95)
        self._thresh_slider.setValue(40)
        self._thresh_slider.setFixedWidth(110)
        self._thresh_slider.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._thresh_slider.valueChanged.connect(self._on_thresh_changed)
        sub_layout.addWidget(self._thresh_slider)
        self._thresh_val_lbl = QLabel('40%')
        self._thresh_val_lbl.setStyleSheet(f'color: {_COLORS["accent"]}; font-size: 12px; font-weight: 600; min-width: 35px;')
        sub_layout.addWidget(self._thresh_val_lbl)
        sub_layout.addSpacing(10)
        reset_btn = QPushButton('Reset Zoom')
        reset_btn.setFixedHeight(32)
        reset_btn.setStyleSheet(
            f'\n            QPushButton {{\n                background: {_COLORS["bg_input"]};\n'
            f'                border: 1px solid {_COLORS["border"]};\n'
            f'                border-radius: 6px;\n                padding: 0 10px;\n'
            f'                font-size: 12px;\n                color: {_COLORS["text"]};\n'
            f'            }}\n            QPushButton:hover {{\n'
            f'                background: {_COLORS["accent"]};\n'
            f'                color: white;\n                border-color: {_COLORS["accent_hover"]};\n'
            f'            }}\n        '
        )
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.clicked.connect(lambda: self._chart.reset_view() if hasattr(self, '_chart') and self._chart else None)
        sub_layout.addWidget(reset_btn)
        layout.addLayout(sub_layout)
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet('background-color: #e2e8f0;')
        layout.addWidget(sep)
        self._chart = DendrogramWidget()
        self._chart.setStyleSheet('background: #f8fafc; border-radius: 8px;')
        layout.addWidget(self._chart, 1)
        footer = QHBoxLayout()
        self._status_lbl = QLabel('')
        self._status_lbl.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 11px;')
        refresh_btn = QPushButton(T('memory.dendrogram_refresh'))
        refresh_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        refresh_btn.setFixedHeight(32)
        refresh_btn.setStyleSheet(
            f'\n            QPushButton {{\n                background: {_COLORS["bg_input"]};\n'
            f'                border: 1px solid {_COLORS["border"]};\n'
            f'                border-radius: 6px;\n                padding: 0 14px;\n'
            f'                font-size: 12px;\n                color: {_COLORS["text"]};\n'
            f'            }}\n            QPushButton:hover {{\n'
            f'                background: {_COLORS["accent"]};\n'
            f'                color: white;\n                border-color: {_COLORS["accent_hover"]};\n'
            f'            }}\n        '
        )
        refresh_btn.clicked.connect(self._fetch_dendrogram)
        footer.addWidget(self._status_lbl)
        footer.addStretch()
        footer.addWidget(refresh_btn)
        layout.addLayout(footer)

    def _on_source_changed(self, idx: int):
        data = self._source_combo.itemData(idx)
        if data and str(data) != self._current_memory_type:
            self._current_memory_type = str(data)
            self._subtitle.setText(T('memory.dendrogram_subtitle').replace('LTM', self._current_memory_type.upper()))
            self._fetch_dendrogram()

    def _fetch_dendrogram(self):
        """Asynchronously loads data from the backend."""
        if not (self._command_handler and self._async_loop):
            self._show_error(T('memory.dendrogram_error', 'Module not ready'))
            return
        self._status_lbl.setText('...')

        async def _run():
            try:
                result = await self._command_handler.handle('get_dendrogram', command='get_dendrogram', memory_type=self._current_memory_type)
                self._result_ready.emit(result)
            except Exception as e:
                self._result_ready.emit(status='error', code='EXCEPTION', error=str(e))

        loop = self._async_loop
        loop.call_soon_threadsafe(asyncio.ensure_future(_run()))

    def _on_result(self, result: dict):
        if not isinstance(result, dict):
            self._show_error(T('memory.dendrogram_error', 'Invalid response'))
            return
        result_source = result.get('memory_type')
        if result_source is not None and str(result_source).lower() != self._current_memory_type:
            return
        if result.get('status') != 'success':
            code = result.get('code', '')
            if code == 'NOT_ENOUGH_DATA':
                self._chart.set_data([], 0, [], [])
                self._indices = []
                self._centers_lbl.setText('')
                n = result.get('n_active', '?')
                nf = result.get('n_active_flag', '?')
                nh = result.get('n_h_positive', '?')
                ntx = result.get('n_texts', '?')
                diag = f'active={nf}, h>0={nh}, texts={ntx}, total={n}'
                msg = T('memory.dendrogram_empty').replace('LTM', self._current_memory_type.upper())
                self._status_lbl.setText(f'{msg} [{diag}]')
            else:
                self._show_error(T('memory.dendrogram_error', result.get('error', '?')))
            return
        try:
            data = _normalize_projection_result(result)
            n = data['n_points']
            Z = data['linkage_matrix']
            if len(Z) != max(0, n - 1):
                raise ValueError('linkage_matrix is not aligned with n_points')
        except ValueError as exc:
            self._show_error(T('memory.dendrogram_error', str(exc)))
            return
        intensities = data['intensities']
        usages = data['usages']
        self._indices = data['indices']
        self._chart.set_data(Z, n, intensities, usages)
        self._centers_lbl.setText(T('memory.dendrogram_centers', n).replace('LTM', self._current_memory_type.upper()))
        self._status_lbl.setText('')

    def _on_thickness_mode_changed(self, idx: int):
        if not hasattr(self, '_chart'):
            return
        if not hasattr(self, '_thickness_combo'):
            return
        mode = self._thickness_combo.itemData(idx)
        if not mode:
            return
        self._chart.set_thickness_mode(str(mode))

    def _on_thresh_changed(self, val: int):
        self._thresh_val_lbl.setText(f'{val}%')
        if not hasattr(self, '_chart'):
            return
        if not self._chart:
            return
        self._chart.set_color_threshold(val / 100)

    def _show_error(self, msg: str):
        self._status_lbl.setText(msg)
        self._indices = []
        self._centers_lbl.setText('')
        self._chart.set_data([], 0, [], [])
        self._chart.set_data([], 0, [], [])


class TemporalEvolutionWidget(QWidget):
    """
    Time axis of cognitive development with a macro (Streamgraph/Swimlanes) and micro (local arrangement) view.
    - X axis: Center Age (Age) - from oldest (left) to newest / present (right).
    - Y axis: Semantic lanes (Swimlanes) derived directly from the Ward dendrogram (fixed trunk).
    - Outlier preservation: Anomalies glow as separate stars outside the main lanes.
    - Cross-domain links: Highlighting connections across topics on hover.
    - Full support for mouse zoom and pan.
    """

    leaf_clicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._linkage = []
        self._n_points = 0
        self._intensities = []
        self._usages = []
        self._ages = []
        self._indices = []
        self._key_texts = []
        self._value_texts = []
        self._view_mode = 'macro'
        self._lane_mode = 'auto'
        self._zoom_x = 1
        self._zoom_y = 1
        self._pan_x = 0
        self._pan_y = 0
        self._drag_active = False
        self._drag_start_pos = QPointF(0, 0)
        self._hovered_idx = -1
        self._node_cluster = {}
        self._node_outlier = {}
        self._cluster_colors = {}
        self._node_rects = []
        self.setMouseTracking(True)
        self.setMinimumSize(500, 350)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_data(self, linkage_matrix: list, n_points: int, intensities: list = None,
                 usages: list = None, ages: list = None, indices: list = None,
                 key_texts: list = None, value_texts: list = None):
        self._linkage = linkage_matrix
        self._n_points = n_points
        self._intensities = intensities or [1] * n_points
        self._usages = usages or [1] * n_points
        self._ages = ages or [0] * n_points
        self._indices = indices or list(range(n_points))
        self._key_texts = key_texts or [''] * n_points
        self._value_texts = value_texts or [''] * n_points
        self._zoom_x = 1
        self._zoom_y = 1
        self._pan_x = 0
        self._pan_y = 0
        self._hovered_idx = -1
        self._compute_layout()
        self.update()

    def set_view_mode(self, mode: str):
        if self._view_mode != mode:
            self._view_mode = mode
            self.update()

    def set_lane_mode(self, mode: str):
        if self._lane_mode != mode:
            self._lane_mode = mode
            self._compute_layout()
            self.update()

    def reset_view(self):
        self._zoom_x = 1
        self._zoom_y = 1
        self._pan_x = 0
        self._pan_y = 0
        self.update()

    def _compute_layout(self):
        self._node_cluster = {}
        self._node_outlier = {}
        self._cluster_colors = {}
        if self._n_points < 1:
            return
        if self._lane_mode == 'auto':
            if self._n_points >= 24:
                n_lanes = max(3, min(8, self._n_points // 8))
            elif self._n_points >= 6:
                n_lanes = max(2, min(5, self._n_points // 3))
            else:
                n_lanes = 1
        else:
            try:
                n_lanes = int(self._lane_mode)
            except (ValueError, TypeError):
                n_lanes = 5
        n_lanes = max(1, min(n_lanes, self._n_points))
        if not self._linkage or self._n_points < 2:
            for i in range(self._n_points):
                self._node_cluster[i] = 0
                self._node_outlier[i] = False
            self._cluster_colors[0] = QColor.fromHsl(200, 190, 130)
            return
        roots = [2 * self._n_points - 2]
        while len(roots) < n_lanes:
            best_idx = -1
            best_dist = -1
            for i, r in enumerate(roots):
                if r >= self._n_points:
                    dist = self._linkage[r - self._n_points][2]
                    if dist > best_dist:
                        best_dist = dist
                        best_idx = i
            if best_idx == -1 or best_dist <= 0:
                break
            r = roots.pop(best_idx)
            left = int(self._linkage[r - self._n_points][0])
            right = int(self._linkage[r - self._n_points][1])
            roots.append(left)
            roots.append(right)
        cluster_sizes = {i: 0 for i in range(len(roots))}
        for c_idx, root in enumerate(roots):
            hue = int(c_idx * 360 / max(1, len(roots))) % 360
            self._cluster_colors[c_idx] = QColor.fromHsl(hue, 190, 130)
            stack = [root]
            while stack:
                node = stack.pop()
                if node < self._n_points:
                    self._node_cluster[node] = c_idx
                    cluster_sizes[c_idx] += 1
                else:
                    stack.append(int(self._linkage[node - self._n_points][0]))
                    stack.append(int(self._linkage[node - self._n_points][1]))
        max_dist = max((row[2] for row in self._linkage), default=1)
        for i in range(self._n_points):
            if self._n_points >= 15 and cluster_sizes.get(self._node_cluster.get(i, 0), 0) <= 2:
                self._node_outlier[i] = True
            else:
                self._node_outlier[i] = False

    def wheelEvent(self, event):
        if self._n_points < 1:
            return
        delta = event.angleDelta().y()
        factor = 1.15 if delta > 0 else 0.869565
        old_zoom_x = self._zoom_x
        old_zoom_y = self._zoom_y
        new_zoom_x = max(1, min(500, old_zoom_x * factor))
        new_zoom_y = max(1, min(10, old_zoom_y * (1 + (factor - 1) * 0.5)))
        if new_zoom_x == old_zoom_x and new_zoom_y == old_zoom_y:
            return
        if hasattr(event, 'position'):
            pos = event.position()
        else:
            pos = event.pos()
        mx, my = pos.x(), pos.y()
        scale_x = new_zoom_x / old_zoom_x
        scale_y = new_zoom_y / old_zoom_y
        self._pan_x = mx - (mx - self._pan_x) * scale_x
        self._pan_y = my - (my - self._pan_y) * scale_y
        self._zoom_x = new_zoom_x
        self._zoom_y = new_zoom_y
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        if hasattr(event, 'position'):
            pos = event.position()
        else:
            pos = event.pos()
        px, py = pos.x(), pos.y()
        if event.button() == Qt.MouseButton.LeftButton:
            for rect, idx in self._node_rects:
                if rect.contains(px, py):
                    self.leaf_clicked.emit(int(idx))
                    return
            self._drag_active = True
            self._drag_start_pos = pos
            return
        if event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._drag_active = True
            self._drag_start_pos = pos
            return

    def mouseMoveEvent(self, event):
        if hasattr(event, 'position'):
            pos = event.position()
        else:
            pos = event.pos()
        px, py = pos.x(), pos.y()
        if self._drag_active:
            dx = px - self._drag_start_pos.x()
            dy = py - self._drag_start_pos.y()
            self._pan_x += dx
            self._pan_y += dy
            self._drag_start_pos = pos
            self.update()
            return
        old_hover = self._hovered_idx
        self._hovered_idx = -1
        tooltip_txt = ''
        for rect, idx in self._node_rects:
            if rect.contains(px, py):
                self._hovered_idx = idx
                age_val = self._ages[idx] if idx < len(self._ages) else 0
                h_val = self._intensities[idx] if idx < len(self._intensities) else 1
                kt = self._key_texts[idx] if idx < len(self._key_texts) else ''
                vt = self._value_texts[idx] if idx < len(self._value_texts) else ''
                label = (kt or vt or f'Center #{idx}')[:60]
                if self._node_outlier.get(idx, False):
                    tooltip_txt = f'Anomaly (Outlier): {label}\nAge: {age_val} | Intensity: {h_val:.2f}'
                else:
                    c_id = self._node_cluster.get(idx, 0)
                    tooltip_txt = f'LTM #{idx} (Lane #{c_id + 1}): {label}\nAge: {age_val} | Intensity: {h_val:.2f}'
                break
        if tooltip_txt:
            QToolTip.showText(QCursor.pos(), tooltip_txt, self)
        if self._hovered_idx != old_hover:
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._drag_active = False

    def mouseDoubleClickEvent(self, event):
        self.reset_view()

    def _get_cognitive_tree(self, hovered_idx: int) -> tuple:
        """
        Returns the set of nodes (tree_nodes) and the list of edges (tree_edges: list[(src, tgt, edge_type)])
        forming the whole temporal cognitive tree for the given hovered_idx.
        """
        if hovered_idx < 0 or hovered_idx >= self._n_points:
            return set(), []
        tree_nodes = {hovered_idx}
        tree_edges = []
        c_hover = self._node_cluster.get(hovered_idx, 0)
        cluster_nodes = [i for i in range(self._n_points)
                         if self._node_cluster.get(i, 0) == c_hover
                         and not self._node_outlier.get(i, False)]
        if self._node_outlier.get(hovered_idx, False) and hovered_idx not in cluster_nodes:
            cluster_nodes.append(hovered_idx)
        cluster_nodes.sort(key=lambda idx: self._ages[idx] if idx < len(self._ages) else 0, reverse=True)
        for idx in cluster_nodes:
            tree_nodes.add(idx)
        for i in range(len(cluster_nodes) - 1):
            tree_edges.append((cluster_nodes[i], cluster_nodes[i + 1], 'backbone'))
        if self._linkage and len(self._linkage) == self._n_points - 1:
            parent_map = {}
            sibling_map = {}
            for r, row in enumerate(self._linkage):
                if len(row) >= 2:
                    p_id = self._n_points + r
                    left = int(row[0])
                    right = int(row[1])
                    parent_map[left] = p_id
                    parent_map[right] = p_id
                    sibling_map[left] = right
                    sibling_map[right] = left
            curr = hovered_idx
            for _ in range(3):
                if curr not in parent_map:
                    break
                sib = sibling_map.get(curr, -1)
                if sib != -1:
                    leaves = []
                    stack = [sib]
                    while stack:
                        nd = stack.pop()
                        if nd < self._n_points:
                            leaves.append(nd)
                        else:
                            r_idx = nd - self._n_points
                            if 0 <= r_idx < len(self._linkage) and len(self._linkage[r_idx]) >= 2:
                                stack.append(int(self._linkage[r_idx][0]))
                                stack.append(int(self._linkage[r_idx][1]))
                    foreign_leaves = [lf for lf in leaves if self._node_cluster.get(lf, 0) != c_hover]
                    if foreign_leaves:
                        h_age = self._ages[hovered_idx] if hovered_idx < len(self._ages) else 0
                        foreign_leaves.sort(key=lambda lf: abs((self._ages[lf] if lf < len(self._ages) else 0) - h_age))
                        best_sib_leaf = foreign_leaves[0]
                        tree_nodes.add(best_sib_leaf)
                        sib_age = self._ages[best_sib_leaf] if best_sib_leaf < len(self._ages) else 0
                        if sib_age >= h_age:
                            tree_edges.append((best_sib_leaf, hovered_idx, 'branch'))
                        else:
                            tree_edges.append((hovered_idx, best_sib_leaf, 'branch'))
                curr = parent_map[curr]
        return tree_nodes, tree_edges

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor('#f8fafc'))
        if self._n_points < 1:
            p.setFont(QFont('Segoe UI', 11))
            p.setPen(QColor('#94a3b8'))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, T('memory.temporal_empty'))
            p.end()
            return
        ml = 130
        mr = 40
        mt = 30
        mb = 45
        cw = max(10, self.width() - ml - mr)
        ch = max(10, self.height() - mt - mb)
        has_outliers = any(self._node_outlier.values())
        h_outlier = int(ch * 0.15) if has_outliers else 0
        h_lanes = ch - h_outlier
        n_lanes = max(1, len(self._cluster_colors))
        lane_h = h_lanes / n_lanes
        max_age = max(self._ages, default=1)
        if max_age <= 0:
            max_age = 1
        self._node_rects = []
        node_centers = {}
        node_info = []
        p.save()
        p.setClipRect(ml, mt, cw, ch)
        for c in range(n_lanes):
            y_top = mt + h_outlier + c * lane_h * self._zoom_y + self._pan_y
            y_bot = y_top + lane_h * self._zoom_y
            if y_bot < mt or y_top > mt + ch:
                continue
            bg_col = QColor('#ffffff') if c % 2 == 0 else QColor('#f1f5f9')
            p.fillRect(QRectF(ml, max(mt, y_top), cw, min(ch, y_bot) - max(mt, y_top)), bg_col)
            p.setPen(QPen(QColor('#cbd5e1'), 1, Qt.PenStyle.DashLine))
            p.drawLine(ml, int(y_top), ml + cw, int(y_top))
            cluster_nodes = [i for i in range(self._n_points)
                             if self._node_cluster.get(i, 0) == c
                             and not self._node_outlier.get(i, False)]
            cluster_nodes.sort(key=lambda idx: self._ages[idx] if idx < len(self._ages) else 0, reverse=True)
            y_center = (y_top + y_bot) / 2
            for order_idx, idx in enumerate(cluster_nodes):
                age_i = self._ages[idx] if idx < len(self._ages) else 0
                x_i = ml + cw * self._zoom_x * (max_age - age_i) / max_age + self._pan_x
                dy = (order_idx % 5 - 2) * lane_h * 0.16 * self._zoom_y
                y_i = y_center + dy
                r = max(4, min(9, 4 * math.log(1 + (self._usages[idx] if idx < len(self._usages) else 1) + (self._intensities[idx] if idx < len(self._intensities) else 1))))
                col = self._cluster_colors.get(c, QColor('#0284c7'))
                rect = QRectF(x_i - r, y_i - r, 2 * r, 2 * r)
                self._node_rects.append((rect, idx))
                node_centers[idx] = QPointF(x_i, y_i)
                node_info.append((idx, rect, r, col, False, QPointF(x_i, y_i)))
        if h_outlier > 0:
            for i in range(self._n_points):
                if not self._node_outlier.get(i, False):
                    continue
                age_i = self._ages[i] if i < len(self._ages) else 0
                x_i = ml + cw * self._zoom_x * (max_age - age_i) / max_age + self._pan_x
                y_i = mt + h_outlier * 0.5 + (i % 3 - 1) * 12 + self._pan_y
                col = QColor('#fbbf24')
                rect = QRectF(x_i - 8, y_i - 8, 16, 16)
                self._node_rects.append((rect, i))
                node_centers[i] = QPointF(x_i, y_i)
                node_info.append((i, rect, 8, col, True, QPointF(x_i, y_i)))
        tree_nodes = set()
        tree_edges = []
        if self._hovered_idx != -1 and 0 <= self._hovered_idx < self._n_points:
            tree_nodes, tree_edges = self._get_cognitive_tree(self._hovered_idx)
        for src_idx, tgt_idx, edge_type in tree_edges:
            if src_idx not in node_centers or tgt_idx not in node_centers:
                continue
            pt_src = node_centers[src_idx]
            pt_tgt = node_centers[tgt_idx]
            path = QPainterPath()
            path.moveTo(pt_src)
            mid_x = (pt_src.x() + pt_tgt.x()) / 2
            path.cubicTo(QPointF(mid_x, pt_src.y()), QPointF(mid_x, pt_tgt.y()), pt_tgt)
            if edge_type == 'backbone':
                c_h = self._node_cluster.get(self._hovered_idx, 0)
                col_edge = self._cluster_colors.get(c_h, QColor('#0284c7'))
                p.setPen(QPen(col_edge, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
                if pt_tgt.x() != pt_src.x() or pt_tgt.y() != pt_src.y():
                    angle = math.atan2(pt_tgt.y() - pt_src.y(), pt_tgt.x() - pt_src.x())
                    arrow_size = 8
                    p1 = pt_tgt - QPointF(arrow_size * math.cos(angle - math.pi / 6), arrow_size * math.sin(angle - math.pi / 6))
                    p2 = pt_tgt - QPointF(arrow_size * math.cos(angle + math.pi / 6), arrow_size * math.sin(angle + math.pi / 6))
                    p.setBrush(col_edge)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.drawPolygon(QPolygonF([pt_tgt, p1, p2]))
            else:
                p.setPen(QPen(QColor('#6366f1'), 2.2, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
        for idx, rect, r, col, is_outlier, pt in node_info:
            if tree_nodes and idx not in tree_nodes:
                p.setOpacity(0.22)
            else:
                p.setOpacity(1)
            if is_outlier:
                star_pts = []
                for k in range(10):
                    angle = k * math.pi / 5 - math.pi / 2
                    rad = 8 if k % 2 == 0 else 3.5
                    if idx == self._hovered_idx:
                        rad += 2.5
                    elif tree_nodes and idx in tree_nodes:
                        rad += 1
                    star_pts.append(QPointF(pt.x() + rad * math.cos(angle), pt.y() + rad * math.sin(angle)))
                star_poly = QPolygonF(star_pts)
                p.setBrush(col)
                if idx == self._hovered_idx:
                    pen = QPen(QColor('#0f172a'), 2.5)
                elif tree_nodes and idx in tree_nodes:
                    pen = QPen(QColor('#b45309'), 2)
                else:
                    pen = QPen(QColor('#d97706'), 1.2)
                p.setPen(pen)
                p.drawPolygon(star_poly)
            else:
                p.setBrush(col)
                if idx == self._hovered_idx:
                    pen = QPen(QColor('#0f172a'), 2.8)
                    draw_r = r + 2.5
                elif tree_nodes and idx in tree_nodes:
                    pen = QPen(QColor('#1e293b'), 2)
                    draw_r = r + 1
                else:
                    pen = QPen(col.darker(140), 1.2)
                    draw_r = r
                p.setPen(pen)
                p.drawEllipse(QRectF(pt.x() - draw_r, pt.y() - draw_r, 2 * draw_r, 2 * draw_r))
            if tree_nodes and idx in tree_nodes and idx != self._hovered_idx and p.opacity() == 1:
                p.setFont(QFont('Segoe UI', 8, QFont.Weight.Bold))
                p.setPen(QColor('#334155'))
                age_val = self._ages[idx] if idx < len(self._ages) else 0
                lbl_txt = f't-{age_val}' if age_val > 0 else 'now'
                p.drawText(QRectF(pt.x() - 25, pt.y() - draw_r - 16, 50, 14), Qt.AlignmentFlag.AlignCenter, lbl_txt)
        p.setOpacity(1)
        p.restore()
        p.fillRect(QRectF(0, mt, ml, ch), QColor('#f8fafc'))
        p.setPen(QPen(QColor('#cbd5e1'), 1))
        p.drawLine(ml, mt, ml, mt + ch)
        if h_outlier > 0:
            star_pts = []
            star_cx, star_cy = 16, mt + h_outlier / 2
            for k in range(10):
                angle = k * math.pi / 5 - math.pi / 2
                rad = 5.5 if k % 2 == 0 else 2.3
                star_pts.append(QPointF(star_cx + rad * math.cos(angle), star_cy + rad * math.sin(angle)))
            p.setBrush(QColor('#fbbf24'))
            p.setPen(QPen(QColor('#d97706'), 1))
            p.drawPolygon(QPolygonF(star_pts))
            p.setFont(QFont('Segoe UI', 9, QFont.Weight.Bold))
            p.setPen(QColor('#d97706'))
            p.drawText(QRectF(28, mt, ml - 34, h_outlier), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, 'Anomaly')
        p.setFont(QFont('Segoe UI', 9, QFont.Weight.Bold))
        for c in range(n_lanes):
            y_top = mt + h_outlier + c * lane_h * self._zoom_y + self._pan_y
            y_bot = y_top + lane_h * self._zoom_y
            if y_bot < mt or y_top > mt + ch:
                continue
            col = self._cluster_colors.get(c, QColor('#0284c7'))
            p.setBrush(col)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(10, (y_top + y_bot) / 2 - 5, 10, 10))
            p.setPen(QColor('#1e293b'))
            p.drawText(QRectF(26, max(mt, y_top), ml - 30, min(ch, y_bot) - max(mt, y_top)), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f'Cluster #{c + 1}')
        p.setPen(QPen(QColor('#cbd5e1'), 1))
        p.drawLine(ml, mt + ch, ml + cw, mt + ch)
        p.setFont(QFont('Segoe UI', 9))
        p.setPen(QColor('#334155'))
        p.drawText(QRectF(ml, mt + ch + 5, 150, 20), Qt.AlignmentFlag.AlignLeft, f'Age: {max_age} (Oldest)')
        p.drawText(QRectF(ml + cw - 150, mt + ch + 5, 150, 20), Qt.AlignmentFlag.AlignRight, 'Present (Age: 0)')
        if self._zoom_x > 1.01 or self._zoom_y > 1.01 or abs(self._pan_x) > 1 or abs(self._pan_y) > 1:
            zoom_txt = f'Zoom: {self._zoom_x:.1f}x × {self._zoom_y:.1f}x | Double-click to reset'
            p.drawText(QRectF(ml, mt - 22, cw, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, zoom_txt)
        p.end()


class TemporalEvolutionWindow(QDialog):
    """
    Standalone window for the cognitive development time axis.
    """

    _result_ready = pyqtSignal(dict)

    def __init__(self, parent, command_handler, async_loop):
        super().__init__(parent)
        self._command_handler = command_handler
        self._async_loop = async_loop
        self._current_memory_type = 'ltm'
        self._indices = []
        self._result_ready.connect(self._on_result)
        self.setWindowTitle(T('memory.temporal_title'))
        self.setMinimumSize(780, 540)
        self.resize(920, 600)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowCloseButtonHint | Qt.WindowType.WindowMaximizeButtonHint)
        self._build_ui()
        self._chart.leaf_clicked.connect(self._on_leaf_clicked)
        QTimer.singleShot(0, self._fetch_temporal_map)

    def _on_leaf_clicked(self, center_idx: int):
        if hasattr(self, '_indices') and center_idx < len(self._indices):
            real_idx = self._indices[center_idx]
        else:
            real_idx = center_idx
        dlg = CenterDetailDialog(self, real_idx, self._command_handler, self._async_loop, memory_type=self._current_memory_type)
        dlg.exec()
        if dlg.center_deleted or getattr(dlg, 'center_updated', False):
            self._fetch_temporal_map()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title_lbl = QLabel(T('memory.temporal_title'))
        title_lbl.setFont(QFont('Segoe UI', 14, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {_COLORS['text']};")
        self._centers_lbl = QLabel('')
        self._centers_lbl.setStyleSheet(f"color: {_COLORS['text_dim']}; font-size: 12px;")
        header.addWidget(title_lbl)
        header.addStretch()
        header.addWidget(self._centers_lbl)
        layout.addLayout(header)
        sub_layout = QHBoxLayout()
        self._subtitle = QLabel(T('memory.temporal_subtitle').replace('LTM', 'STM'))
        self._subtitle.setStyleSheet(f"color: {_COLORS['text_dim']}; font-size: 12px;")
        sub_layout.addWidget(self._subtitle)
        sub_layout.addStretch()
        self._source_lbl = QLabel(T('memory.select_source'))
        self._source_lbl.setStyleSheet(f"color: {_COLORS['text']}; font-size: 12px; font-weight: 600;")
        sub_layout.addWidget(self._source_lbl)
        self._source_combo = QComboBox()
        self._source_combo.addItem('STM', 'stm')
        self._source_combo.addItem('LTM', 'ltm')
        self._source_combo.setCurrentIndex(self._source_combo.findData(self._current_memory_type))
        self._source_combo.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._source_combo.setFixedHeight(32)
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)
        sub_layout.addWidget(self._source_combo)
        sub_layout.addSpacing(14)
        lbl_lanes = QLabel(T('memory.temporal_lanes_label'))
        lbl_lanes.setStyleSheet(f"color: {_COLORS['text']}; font-size: 12px; font-weight: 600;")
        sub_layout.addWidget(lbl_lanes)
        self._lanes_combo = QComboBox()
        self._lanes_combo.addItem(T('memory.temporal_lanes_auto'), 'auto')
        self._lanes_combo.addItem('3', '3')
        self._lanes_combo.addItem('5', '5')
        self._lanes_combo.addItem('7', '7')
        self._lanes_combo.addItem('10', '10')
        self._lanes_combo.setFixedHeight(32)
        self._lanes_combo.currentIndexChanged.connect(self._on_lanes_changed)
        sub_layout.addWidget(self._lanes_combo)
        reset_btn = QPushButton(T('memory.temporal_reset_zoom'))
        reset_btn.setFixedHeight(32)
        reset_btn.setStyleSheet(f'''
            QPushButton {{
                background: {_COLORS['bg_input']};
                border: 1px solid {_COLORS['border']};
                border-radius: 6px;
                padding: 0 14px;
                font-size: 12px;
                color: {_COLORS['text']};
            }}
            QPushButton:hover {{
                background: {_COLORS['accent']};
                color: white;
                border-color: {_COLORS['accent_hover']};
            }}
        ''')
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.clicked.connect(lambda: self._chart.reset_view())
        sub_layout.addWidget(reset_btn)
        layout.addLayout(sub_layout)
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet('background-color: #e2e8f0;')
        layout.addWidget(sep)
        self._chart = TemporalEvolutionWidget()
        self._chart.setStyleSheet('background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px;')
        layout.addWidget(self._chart, 1)
        footer = QHBoxLayout()
        self._status_lbl = QLabel('')
        self._status_lbl.setStyleSheet(f"color: {_COLORS['text_dim']}; font-size: 11px;")
        refresh_btn = QPushButton(T('memory.temporal_refresh'))
        refresh_btn.setFixedHeight(32)
        refresh_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        refresh_btn.setStyleSheet(f'''
            QPushButton {{
                background: {_COLORS['bg_input']};
                border: 1px solid {_COLORS['border']};
                border-radius: 6px;
                padding: 0 14px;
                font-size: 12px;
                color: {_COLORS['text']};
            }}
            QPushButton:hover {{
                background: {_COLORS['accent']};
                color: white;
                border-color: {_COLORS['accent_hover']};
            }}
        ''')
        refresh_btn.clicked.connect(self._fetch_temporal_map)
        footer.addWidget(self._status_lbl)
        footer.addStretch()
        footer.addWidget(refresh_btn)
        layout.addLayout(footer)

    def _on_source_changed(self, idx: int):
        data = self._source_combo.itemData(idx)
        if data and str(data) != self._current_memory_type:
            self._current_memory_type = str(data)
            self._subtitle.setText(T('memory.temporal_subtitle').replace('LTM', self._current_memory_type.upper()))
            self._fetch_temporal_map()

    def _on_lanes_changed(self, idx: int):
        mode = self._lanes_combo.itemData(idx)
        if mode:
            self._chart.set_lane_mode(str(mode))

    def _fetch_temporal_map(self):
        if not self._command_handler or not self._async_loop:
            self._show_error(T('memory.temporal_error', 'Module not ready'))
            return
        self._status_lbl.setText('...')

        async def _run():
            try:
                result = await self._command_handler.handle(command='get_temporal_evolution_map', memory_type=self._current_memory_type)
                self._result_ready.emit(result)
            except Exception as e:
                self._result_ready.emit({'status': 'error', 'code': 'EXCEPTION', 'error': str(e)})

        self._async_loop.call_soon_threadsafe(asyncio.ensure_future(_run()))

    def _on_result(self, result: dict):
        if not isinstance(result, dict):
            self._show_error(T('memory.temporal_error', 'Invalid response'))
            return
        result_source = result.get('memory_type')
        if result_source is not None and str(result_source).lower() != self._current_memory_type:
            return
        if result.get('status') != 'success':
            code = result.get('code', '')
            if code == 'NOT_ENOUGH_DATA':
                self._chart.set_data([], 0)
                self._indices = []
                self._centers_lbl.setText('')
                n = result.get('n_active', '?')
                nf = result.get('n_active_flag', '?')
                nh = result.get('n_h_positive', '?')
                ntx = result.get('n_texts', '?')
                diag = f'active={nf}, h>0={nh}, texts={ntx}, total={n}'
                msg = T('memory.temporal_empty').replace('LTM', self._current_memory_type.upper())
                self._status_lbl.setText(f'{msg} [{diag}]')
            else:
                self._show_error(T('memory.temporal_error', result.get('error', '?')))
            return
        try:
            data = _normalize_projection_result(result)
            n = data['n_points']
            Z = data['linkage_matrix']
            if len(Z) != max(0, n - 1):
                raise ValueError('linkage_matrix is not aligned with n_points')
        except ValueError as exc:
            self._show_error(T('memory.temporal_error', str(exc)))
            return
        intensities = data['intensities']
        usages = data['usages']
        ages = data['ages']
        indices = data['indices']
        key_texts = data['key_texts']
        value_texts = data['value_texts']
        self._indices = indices
        self._chart.set_data(Z, n, intensities, usages, ages, indices, key_texts, value_texts)
        self._centers_lbl.setText(T('memory.temporal_centers', n).replace('LTM', self._current_memory_type.upper()))
        self._status_lbl.setText('')

    def _show_error(self, msg: str):
        self._status_lbl.setText(msg)
        self._indices = []
        self._centers_lbl.setText('')
        self._chart.set_data([], 0)


class GraphNodeItem(QGraphicsEllipseItem):
    """Custom graphic element for a node (center) with highlight and detail."""

    def __init__(self, idx: int, center_id: int, label_text: str, radius: float, color: QColor, intensity: float, usage: int):
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.idx = idx
        self.center_id = center_id
        self.label_text = label_text
        self.radius = radius
        self.base_color = color
        self.intensity = intensity
        self.usage = usage
        self.edges = []
        self.setAcceptHoverEvents(True)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setBrush(QBrush(self.base_color))
        self.setPen(QPen(QColor('#ffffff'), 2))
        self.setZValue(2)
        self.text_item = QGraphicsTextItem(f'#{self.center_id}\n{self.label_text}', self)
        font = QFont('Segoe UI', 9, QFont.Weight.Bold)
        self.text_item.setFont(font)
        self.text_item.setDefaultTextColor(QColor('#1e293b'))
        br = self.text_item.boundingRect()
        self.text_item.setPos(-br.width() / 2, radius + 2)

    def hoverEnterEvent(self, event):
        super().hoverEnterEvent(event)
        if self.scene() and hasattr(self.scene(), 'view') and self.scene().view and hasattr(self.scene().view, 'highlight_node'):
            view = self.scene().view
            view.highlight_node(self)
        QToolTip.showText(event.screenPos(), f'Center #{self.center_id}\nWeight: {self.intensity:.2f} • Accesses: {self.usage}\nText: {self.label_text}')

    def hoverLeaveEvent(self, event):
        super().hoverLeaveEvent(event)
        if self.scene() and hasattr(self.scene(), 'view') and self.scene().view and hasattr(self.scene().view, 'reset_highlight'):
            view = self.scene().view
            view.reset_highlight()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.scene() and hasattr(self.scene(), 'view') and self.scene().view and hasattr(self.scene().view, 'node_clicked'):
            view = self.scene().view
            view.node_clicked.emit(self.idx)
        super().mousePressEvent(event)


class GraphMapWidget(QGraphicsView):
    node_clicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.view = self._scene
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet('background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px;')
        self._nodes = []
        self._edges = []
        self._raw_edges = []
        self._min_sim = 0.45

    def set_data(self, n_points: int, intensities: list, usages: list, ages: list, indices: list, key_texts: list, value_texts: list, edges: list, memory_type: str = 'ltm'):
        self._scene.clear()
        self._nodes.clear()
        self._edges.clear()
        self._raw_edges = edges if edges else []
        if n_points < 1:
            return
        pos = self._compute_layout(n_points, self._raw_edges)
        if memory_type.lower() == 'stm':
            base_rgb = QColor('#0284c7')
        else:
            base_rgb = QColor('#d97706')
        max_int = max(intensities) if intensities and max(intensities) > 0 else 1
        max_use = max(usages) if usages and max(usages) > 0 else 1
        for i in range(n_points):
            if i < len(indices):
                cid = indices[i]
            else:
                cid = i
            txt = ''
            if i < len(key_texts):
                txt = key_texts[i] if key_texts[i] else (value_texts[i] if value_texts[i] else '')
            txt = txt.strip()
            if len(txt) > 18:
                txt = txt[:15] + '...'
            if i < len(intensities):
                intensity = intensities[i]
            else:
                intensity = 1
            if i < len(usages):
                usage = usages[i]
            else:
                usage = 1
            norm_w = 0.6 * (intensity / max_int) + 0.4 * min(1, usage / max_use)
            radius = 11 + norm_w * 16
            node = GraphNodeItem(i, cid, txt, radius, base_rgb, intensity, usage)
            node.setPos(QPointF(pos[i][0], pos[i][1]))
            self._scene.addItem(node)
            self._nodes.append(node)
        self.apply_sim_filter(self._min_sim)
        self._fit_to_nodes()

    def _compute_layout(self, n: int, edges: list) -> list:
        radius = max(200, float(n) * 35 / (2 * math.pi))
        pos = []
        for i in range(n):
            angle = 2 * math.pi * i / n
            pos.append([radius * math.cos(angle), radius * math.sin(angle)])
        if n <= 1:
            return pos
        area = (2 * radius) ** 2
        k = math.sqrt(area / n) * 1.2
        t = radius * 0.5
        dt = t / 35
        adj = {i: {} for i in range(n)}
        for e in edges:
            u, v, w = e[0], e[1], e[2]
            if u < n and v < n:
                adj[u][v] = w
                adj[v][u] = w
        for _ in range(35):
            disp = [[0, 0] for _ in range(n)]
            for i in range(n):
                for j in range(i + 1, n):
                    dx = pos[i][0] - pos[j][0]
                    dy = pos[i][1] - pos[j][1]
                    dist = math.sqrt(dx * dx + dy * dy)
                    if dist < 0.1:
                        dist = 0.1
                        dx, dy = (0.1, 0)
                    force = k * k / dist
                    fx = dx / dist * force
                    fy = dy / dist * force
                    disp[i][0] += fx
                    disp[i][1] += fy
                    disp[j][0] -= fx
                    disp[j][1] -= fy
            for u in range(n):
                for v, w in adj[u].items():
                    if u < v and w >= 0.35:
                        dx = pos[u][0] - pos[v][0]
                        dy = pos[u][1] - pos[v][1]
                        dist = math.sqrt(dx * dx + dy * dy)
                        if dist < 0.1:
                            dist = 0.1
                        force = dist * dist / (k / (w * w))
                        fx = dx / dist * force
                        fy = dy / dist * force
                        disp[u][0] -= fx
                        disp[u][1] -= fy
                        disp[v][0] += fx
                        disp[v][1] += fy
            for i in range(n):
                dx = disp[i][0]
                dy = disp[i][1]
                dist = math.sqrt(dx * dx + dy * dy)
                if dist > 0:
                    step = min(dist, t)
                    pos[i][0] += dx / dist * step
                    pos[i][1] += dy / dist * step
            t = max(2, t - dt)
        return pos

    def apply_sim_filter(self, min_sim: float):
        self._min_sim = min_sim
        for edge_item in self._edges:
            if edge_item.scene():
                self._scene.removeItem(edge_item)
        self._edges.clear()
        for node in self._nodes:
            node.edges.clear()
        for e in self._raw_edges:
            u, v, w = e[0], e[1], e[2]
            if w >= min_sim and u < len(self._nodes) and v < len(self._nodes):
                node_u = self._nodes[u]
                node_v = self._nodes[v]
                line_item = QGraphicsLineItem(node_u.pos().x(), node_u.pos().y(), node_v.pos().x(), node_v.pos().y())
                alpha = int(min(255, max(40, (w - 0.3) / 0.7 * 215)))
                width = 1 + (w - 0.4) * 4
                pen = QPen(QColor(100, 116, 139, alpha), width)
                line_item.setPen(pen)
                line_item.setData(0, w)
                line_item.setZValue(1)
                self._scene.addItem(line_item)
                self._edges.append(line_item)
                node_u.edges.append((line_item, node_v))
                node_v.edges.append((line_item, node_u))

    def highlight_node(self, target_node: GraphNodeItem):
        connected = {target_node}
        for edge_item, other_node in target_node.edges:
            connected.add(other_node)
            edge_item.setPen(QPen(QColor('#3b82f6'), max(2.5, edge_item.pen().widthF() * 1.3)))
            edge_item.setZValue(1.5)
        for node in self._nodes:
            if node in connected:
                node.setOpacity(1)
                if node == target_node:
                    node.setPen(QPen(QColor('#10b981'), 3))
            else:
                node.setOpacity(0.2)
        for edge in self._edges:
            is_connected = any(e[0] == edge for e in target_node.edges)
            if not is_connected:
                edge.setOpacity(0.15)

    def reset_highlight(self):
        for node in self._nodes:
            node.setOpacity(1)
            node.setPen(QPen(QColor('#ffffff'), 2))
        for edge in self._edges:
            edge.setOpacity(1)
            edge.setZValue(1)
            w = edge.data(0)
            if w is None:
                w = (edge.pen().widthF() - 1) / 4 + 0.4
            alpha = int(min(255, max(40, (w - 0.3) / 0.7 * 215)))
            width = 1 + (w - 0.4) * 4
            edge.setPen(QPen(QColor(100, 116, 139, alpha), width))

    def _update_node_scales(self):
        current_zoom = self.transform().m11()
        base_zoom = getattr(self, '_base_zoom', current_zoom)
        if base_zoom > 0 and current_zoom > 0:
            relative_zoom = current_zoom / base_zoom
            node_scale = math.pow(relative_zoom, -0.4)
            for node in self._nodes:
                node.setScale(node_scale)

    def _fit_to_nodes(self):
        if not self._nodes:
            return
        for node in self._nodes:
            node.setScale(1)
        rect = self._scene.itemsBoundingRect()
        if not rect.isEmpty():
            rect.adjust(-80, -80, 80, 80)
            self._scene.setSceneRect(rect)
            self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
            self._base_zoom = self.transform().m11()
            self._update_node_scales()

    def reset_view(self):
        self._fit_to_nodes()

    def wheelEvent(self, event):
        zoom_in_factor = 1.15
        zoom_out_factor = 1 / zoom_in_factor
        if event.angleDelta().y() > 0:
            self.scale(zoom_in_factor, zoom_in_factor)
        else:
            self.scale(zoom_out_factor, zoom_out_factor)
        self._update_node_scales()


class GraphMapWindow(QDialog):
    """
    Standalone non-blocking window with the semantic map of centers (Graph Explorer).
    """

    _result_ready = pyqtSignal(dict)

    def __init__(self, parent, command_handler, async_loop):
        super().__init__(parent)
        self._command_handler = command_handler
        self._async_loop = async_loop
        self._current_memory_type = 'ltm'
        self._indices = []
        self._edge_records = []
        self._detail_dialogs = []
        self._result_ready.connect(self._on_result)
        self.setWindowTitle(T('memory.graph_title'))
        self.setMinimumSize(700, 500)
        self.resize(880, 580)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowCloseButtonHint | Qt.WindowType.WindowMaximizeButtonHint)
        self._build_ui()
        self._chart.node_clicked.connect(self._on_node_clicked)
        QTimer.singleShot(0, self._fetch_graph)

    def _on_node_clicked(self, idx: int):
        active_dialogs = []
        for d in self._detail_dialogs:
            try:
                if hasattr(d, 'isVisible') and d.isVisible():
                    active_dialogs.append(d)
            except RuntimeError:
                pass
        self._detail_dialogs = active_dialogs
        if idx < len(self._indices):
            real_idx = self._indices[idx]
        else:
            real_idx = idx
        dlg = CenterDetailDialog(self, real_idx, self._command_handler, self._async_loop, memory_type=self._current_memory_type)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        def _on_closed():
            if hasattr(dlg, 'center_deleted') and dlg.center_deleted or getattr(dlg, 'center_updated', False):
                self._fetch_graph()

        dlg.finished.connect(_on_closed)
        dlg.show()
        self._detail_dialogs.append(dlg)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title_lbl = QLabel(T('memory.graph_title'))
        title_lbl.setFont(QFont('Segoe UI', 14, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {_COLORS['text']};")
        self._centers_lbl = QLabel('')
        self._centers_lbl.setStyleSheet(f"color: {_COLORS['text_dim']}; font-size: 12px;")
        header.addWidget(title_lbl)
        header.addStretch()
        header.addWidget(self._centers_lbl)
        layout.addLayout(header)
        sub_layout = QHBoxLayout()
        self._subtitle = QLabel(T('memory.graph_subtitle'))
        self._subtitle.setStyleSheet(f"color: {_COLORS['text_dim']}; font-size: 12px;")
        sub_layout.addWidget(self._subtitle)
        sub_layout.addStretch()
        self._source_lbl = QLabel(T('memory.select_source'))
        self._source_lbl.setStyleSheet(f"color: {_COLORS['text']}; font-size: 12px; font-weight: 600;")
        sub_layout.addWidget(self._source_lbl)
        self._source_combo = QComboBox()
        self._source_combo.addItem('STM', 'stm')
        self._source_combo.addItem('LTM', 'ltm')
        self._source_combo.setCurrentIndex(self._source_combo.findData(self._current_memory_type))
        self._source_combo.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._source_combo.setFixedHeight(32)
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)
        sub_layout.addWidget(self._source_combo)
        sub_layout.addSpacing(14)
        self._sim_lbl = QLabel(T('memory.graph_min_sim'))
        self._sim_lbl.setStyleSheet(f"color: {_COLORS['text']}; font-size: 12px; font-weight: 600;")
        sub_layout.addWidget(self._sim_lbl)
        self._sim_slider = QSlider(Qt.Orientation.Horizontal)
        self._sim_slider.setMinimum(30)
        self._sim_slider.setMaximum(90)
        self._sim_slider.setValue(45)
        self._sim_slider.setFixedWidth(110)
        self._sim_slider.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._sim_slider.valueChanged.connect(self._on_sim_changed)
        sub_layout.addWidget(self._sim_slider)
        self._sim_val_lbl = QLabel('0.45')
        self._sim_val_lbl.setStyleSheet(f"color: {_COLORS['text_dim']}; font-size: 12px; width: 32px;")
        sub_layout.addWidget(self._sim_val_lbl)
        reset_btn = QPushButton('Reset Zoom')
        reset_btn.setFixedHeight(32)
        reset_btn.setStyleSheet(f'''
            QPushButton {{
                background: {_COLORS['bg_input']};
                border: 1px solid {_COLORS['border']};
                border-radius: 6px;
                padding: 0 10px;
                font-size: 12px;
                color: {_COLORS['text']};
            }}
            QPushButton:hover {{
                background: {_COLORS['accent']};
                color: white;
                border-color: {_COLORS['accent_hover']};
            }}
        ''')
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.clicked.connect(lambda: self._chart.reset_view())
        sub_layout.addWidget(reset_btn)
        layout.addLayout(sub_layout)
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet('background-color: #e2e8f0;')
        layout.addWidget(sep)
        self._chart = GraphMapWidget()
        layout.addWidget(self._chart, 1)
        footer = QHBoxLayout()
        self._status_lbl = QLabel('')
        self._status_lbl.setStyleSheet(f"color: {_COLORS['text_dim']}; font-size: 11px;")
        refresh_btn = QPushButton(T('memory.graph_refresh'))
        refresh_btn.setFixedHeight(32)
        refresh_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        refresh_btn.setStyleSheet(f'''
            QPushButton {{
                background: {_COLORS['bg_input']};
                border: 1px solid {_COLORS['border']};
                border-radius: 6px;
                padding: 0 14px;
                font-size: 12px;
                color: {_COLORS['text']};
            }}
            QPushButton:hover {{
                background: {_COLORS['accent']};
                color: white;
                border-color: {_COLORS['accent_hover']};
            }}
        ''')
        refresh_btn.clicked.connect(self._fetch_graph)
        footer.addWidget(self._status_lbl)
        footer.addStretch()
        footer.addWidget(refresh_btn)
        layout.addLayout(footer)

    def _on_source_changed(self, idx: int):
        data = self._source_combo.itemData(idx)
        if data and str(data) != self._current_memory_type:
            self._current_memory_type = str(data)
            self._fetch_graph()

    def _on_sim_changed(self, val: int):
        sim = val / 100
        self._sim_val_lbl.setText(f'{sim:.2f}')
        if hasattr(self, '_chart') and self._chart:
            self._chart.apply_sim_filter(sim)

    def _fetch_graph(self):
        if not self._command_handler or not self._async_loop:
            self._show_error(T('memory.graph_error', 'Module not ready'))
            return
        self._status_lbl.setText('...')

        async def _run():
            try:
                result = await self._command_handler.handle(command='get_memory_graph', memory_type=self._current_memory_type)
                self._result_ready.emit(result)
            except Exception as e:
                self._result_ready.emit({'status': 'error', 'code': 'EXCEPTION', 'error': str(e)})

        loop = self._async_loop
        loop.call_soon_threadsafe(asyncio.ensure_future(_run()))

    def _on_result(self, result: dict):
        if not isinstance(result, dict):
            self._show_error(T('memory.graph_error', 'Invalid response'))
            return
        result_source = result.get('memory_type')
        if result_source is not None and str(result_source).lower() != self._current_memory_type:
            return
        if result.get('status') != 'success':
            code = result.get('code', '')
            if code == 'NOT_ENOUGH_DATA':
                self._chart.set_data(0, [], [], [], [], [], [], [], self._current_memory_type)
                self._indices = []
                self._edge_records = []
                self._centers_lbl.setText('')
                self._status_lbl.setText(T('memory.graph_empty'))
            else:
                self._show_error(T('memory.graph_error', result.get('error', '?')))
            return
        try:
            data = _normalize_projection_result(result)
        except ValueError as exc:
            self._show_error(T('memory.graph_error', str(exc)))
            return
        n = data['n_points']
        intensities = data['intensities']
        usages = data['usages']
        ages = data['ages']
        indices = data['indices']
        key_texts = data['key_texts']
        value_texts = data['value_texts']
        edges = data['edges']
        self._indices = indices
        self._edge_records = data['edge_records']
        self._chart.set_data(n, intensities, usages, ages, indices, key_texts, value_texts, edges, self._current_memory_type)
        self._centers_lbl.setText(f'{n} center ({self._current_memory_type.upper()})')
        self._status_lbl.setText('')

    def _show_error(self, msg: str):
        self._status_lbl.setText(msg)
        self._indices = []
        self._edge_records = []
        self._centers_lbl.setText('')
        self._chart.set_data(0, [], [], [], [], [], [], [], self._current_memory_type)


class DashboardSignals(QObject):
    message_received = pyqtSignal(str, dict)


class BDBMDashboard(QMainWindow):

    def __init__(self, settings_manager, on_quit: Optional[Callable] = None,
                 on_show_dashboard: Optional[Callable] = None, conversation_handler=None):
        self._ensure_app()
        super().__init__()
        self._settings = settings_manager
        self._on_quit = on_quit
        self._conv_handler = conversation_handler
        self.signals = DashboardSignals()
        self.signals.message_received.connect(self._process_message)
        self._server_ready = False
        self._memory_stats = {}
        self._async_loop = None
        self._server_task = None
        self._shutdown_requested = False
        self._pending_attachments = []
        self._dendrogram_dlg = None
        self._temporal_dlg = None
        self._thinking_timer = QTimer(self)
        self._thinking_timer.setInterval(1200)
        self._thinking_timer.timeout.connect(self._on_thinking_tick)
        self._thinking_phrases = [
            T('ui.thinking'),
            T('ui.remembering'),
            T('ui.boondoggling'),
            T('ui.shifting'),
            T('ui.seriousing'),
        ]
        self._thinking_index = 0
        self.setWindowTitle('biomem')
        self.resize(980, 640)
        self.setMinimumSize(860, 560)
        app = QApplication.instance()
        app.setStyleSheet(STYLESHEET)
        app.setQuitOnLastWindowClosed(False)
        self._set_window_icon()
        self._build_ui()
        self._load_settings()
        if conversation_handler:
            self._connect_conv_handler(conversation_handler)

    @classmethod
    def _ensure_app(cls):
        if QApplication.instance() is None:
            cls._app = QApplication(sys.argv)

    def mainloop(self):
        app = QApplication.instance()
        sys.exit(app.exec())

    def after(self, ms: int, func: Callable):
        QTimer.singleShot(ms, func)

    def _set_window_icon(self):
        candidates = [
            Path(getattr(sys, '_MEIPASS', '.')) / 'icon.ico',
            Path(sys.executable).parent / 'icon.ico',
            Path(__file__).parent.parent / 'installer' / 'icon.ico',
        ]
        for p in candidates:
            if p.exists():
                self.setWindowIcon(QIcon(str(p)))
                return

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def show_dashboard(self):
        super().show()
        self.raise_()
        self.activateWindow()

    show = show_dashboard

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(24, 20, 24, 20)
        main_layout.setSpacing(12)
        header = QHBoxLayout()
        logo_path = self._find_logo('bdbm_logo.png') or self._find_logo('logokonik.png')
        logo_lbl = QLabel()
        if logo_path:
            pixmap = QPixmap(str(logo_path)).scaledToHeight(36, Qt.TransformationMode.SmoothTransformation)
            logo_lbl.setPixmap(pixmap)
        else:
            logo_lbl.setText('∞')
            logo_lbl.setFont(QFont('Segoe UI', 22, QFont.Weight.Bold))
        self.title_lbl = QLabel(T('ui.title_sub'))
        self.title_lbl.setFont(QFont('Segoe UI', 12, QFont.Weight.Normal))
        self.title_lbl.setStyleSheet(f'color: {_COLORS["text_dim"]};')
        from . import __version__
        version = QLabel(f'v{__version__}')
        version.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 11px;')
        title_col = QWidget()
        _tc_layout = QVBoxLayout(title_col)
        _tc_layout.setContentsMargins(0, 4, 0, 0)
        _tc_layout.setSpacing(1)
        _tc_layout.addWidget(self.title_lbl)
        _tc_layout.addWidget(version)
        header.addWidget(logo_lbl)
        header.addSpacing(8)
        header.addWidget(title_col)
        header.addStretch()
        self.lang_layout = QHBoxLayout()
        self.lang_layout.setSpacing(5)
        self.btn_en = QPushButton('EN')
        self.btn_cz = QPushButton('CZ')
        self.btn_de = QPushButton('DE')
        self.btn_fr = QPushButton('FR')
        self.btn_pl = QPushButton('PL')
        lang_btn_ss = '\n            QPushButton {\n                background: transparent;\n                color: #94a3b8;\n                border: 1px solid #e2e8f0;\n                border-radius: 4px;\n                padding: 4px 6px;\n                font-size: 11px;\n                font-weight: bold;\n                min-width: 32px;\n            }\n            QPushButton:hover {\n                color: #475569;\n                background: #f1f5f9;\n            }\n            QPushButton[active="true"] {\n                color: #0ea5e9;\n                border: 1px solid #0ea5e9;\n                background: #f0f9ff;\n            }\n        '
        for btn, lang_code in ((self.btn_en, 'en'), (self.btn_cz, 'cz'), (self.btn_de, 'de'), (self.btn_fr, 'fr'), (self.btn_pl, 'pl')):
            btn.setStyleSheet(lang_btn_ss)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda _, l=lang_code: self._on_lang_changed(l))
            self.lang_layout.addWidget(btn)
        header.addLayout(self.lang_layout)
        self._update_lang_buttons()
        main_layout.addLayout(header)
        sep = QFrame()
        sep.setFixedHeight(2)
        sep.setStyleSheet('\n            QFrame {\n                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,\n                    stop:0 #f8fafc, stop:0.2 #0ea5e9, stop:0.8 #0284c7, stop:1 #f8fafc);\n                border: none;\n            }\n        ')
        main_layout.addWidget(sep)
        main_layout.addSpacing(6)
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        self.tab_chat = QWidget()
        self.tab_module = QWidget()
        self.tab_memory = QWidget()
        self.tab_llm = QWidget()
        self.tab_news = QWidget()
        self.tabs.addTab(self.tab_chat, T('nav.chat'))
        self.tabs.addTab(self.tab_module, T('nav.module'))
        self.tabs.addTab(self.tab_memory, T('nav.memory'))
        self.tabs.addTab(self.tab_llm, T('nav.llm_settings'))
        self.tabs.addTab(self.tab_news, T('nav.news'))
        self._build_tab_chat()
        self._build_tab_module()
        self._build_tab_memory()
        self._build_tab_llm()
        self._build_tab_news()
        self.tabs.setCurrentIndex(1)

    def _find_logo(self, filename: str) -> Optional[Path]:
        candidates = [
            Path(getattr(sys, '_MEIPASS', '.')) / 'assets' / filename,
            Path(__file__).parent / 'assets' / filename,
        ]
        for p in candidates:
            if p.exists():
                return p
        return None

    def _build_tab_chat(self):
        outer = QHBoxLayout(self.tab_chat)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName('sidebarFrame')
        sidebar.setFixedWidth(210)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(12, 12, 12, 12)
        sb_layout.setSpacing(8)
        self.chat_model_lbl = QLabel(T('ui.model'))
        self.chat_model_lbl.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px; font-weight: 600;')
        self.model_combo = QComboBox()
        self.model_combo.addItems(['ChatGPT', 'Gemini', 'Claude', 'Ollama'])
        self.model_combo.setObjectName('modelCombo')
        sb_layout.addWidget(self.chat_model_lbl)
        sb_layout.addWidget(self.model_combo)
        sb_layout.addSpacing(6)
        self.new_chat_btn = QPushButton(T('ui.new_chat'))
        self.new_chat_btn.setObjectName('actionBtn')
        self.new_chat_btn.setFixedHeight(34)
        self.new_chat_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.new_chat_btn.clicked.connect(self._on_new_chat)
        sb_layout.addWidget(self.new_chat_btn)
        self.hist_lbl = QLabel(T('ui.recent_chats'))
        self.hist_lbl.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px; font-weight: 600;')
        sb_layout.addWidget(self.hist_lbl)
        self.thread_list = QListWidget()
        self.thread_list.setMaximumHeight(200)
        self.thread_list.itemClicked.connect(self._on_thread_selected)
        self.thread_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.thread_list.customContextMenuRequested.connect(self._on_thread_context_menu)
        sb_layout.addWidget(self.thread_list)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f'color: {_COLORS["border"]};')
        sb_layout.addWidget(sep)
        self.chat_mem_lbl = QLabel(T('ui.memory'))
        self.chat_mem_lbl.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px; font-weight: 600;')
        self.chat_ltm_lbl = QLabel('LTM: —')
        self.chat_ltm_lbl.setStyleSheet(f'font-size: 12px; color: {_COLORS["text"]};')
        self.chat_stm_lbl = QLabel('STM: —')
        self.chat_stm_lbl.setStyleSheet(f'font-size: 12px; color: {_COLORS["text"]};')
        self.chat_fatigue_lbl = QLabel('Fatigue: —')
        self.chat_fatigue_lbl.setStyleSheet(f'font-size: 12px; color: {_COLORS["text_dim"]};')
        sb_layout.addWidget(self.chat_mem_lbl)
        sb_layout.addWidget(self.chat_ltm_lbl)
        sb_layout.addWidget(self.chat_stm_lbl)
        sb_layout.addWidget(self.chat_fatigue_lbl)
        dot_row = QHBoxLayout()
        self.chat_status_dot = QLabel('●')
        self.chat_status_dot.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 14px;')
        self.chat_status_lbl = QLabel(T('ui.starting'))
        self.chat_status_lbl.setStyleSheet(f'font-size: 12px; color: {_COLORS["text_dim"]};')
        dot_row.addWidget(self.chat_status_dot)
        dot_row.addWidget(self.chat_status_lbl)
        dot_row.addStretch()
        sb_layout.addLayout(dot_row)
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f'color: {_COLORS["border"]};')
        sb_layout.addWidget(sep2)
        self.chat_options_lbl = QLabel(T('ui.options'))
        self.chat_options_lbl.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px; font-weight: 600;')
        sb_layout.addWidget(self.chat_options_lbl)
        _toggle_lbl_ss = f'font-size: 13px; color: {_COLORS["text"]};'
        dr_row = QHBoxLayout()
        dr_row.setSpacing(10)
        self.deep_recall_toggle = ToggleSwitch()
        self.deep_recall_toggle.setToolTip(T('ui.deep_recall_tip'))
        self.dr_lbl = QLabel(T('ui.deep_recall'))
        self.dr_lbl.setStyleSheet(_toggle_lbl_ss)
        self.dr_lbl.setToolTip(T('ui.deep_recall_tip'))
        dr_row.addWidget(self.deep_recall_toggle)
        dr_row.addWidget(self.dr_lbl)
        dr_row.addStretch()
        sb_layout.addLayout(dr_row)
        ws_row = QHBoxLayout()
        ws_row.setSpacing(10)
        self.web_search_toggle = ToggleSwitch()
        self.web_search_toggle.setToolTip(T('ui.web_search_tip'))
        self.ws_lbl = QLabel(T('ui.web_search'))
        self.ws_lbl.setStyleSheet(_toggle_lbl_ss)
        self.ws_lbl.setToolTip(T('ui.web_search_tip'))
        ws_row.addWidget(self.web_search_toggle)
        ws_row.addWidget(self.ws_lbl)
        ws_row.addStretch()
        sb_layout.addLayout(ws_row)
        sb_layout.addStretch()
        outer.addWidget(sidebar)
        chat_area = QWidget()
        chat_layout = QVBoxLayout(chat_area)
        chat_layout.setContentsMargins(12, 0, 0, 0)
        chat_layout.setSpacing(8)
        self.chat_display = QScrollArea()
        self.chat_display.setWidgetResizable(True)
        self.chat_display.setFrameShape(QFrame.Shape.NoFrame)
        self.chat_display.setStyleSheet(f'\n            QScrollArea {{\n                background-color: #ffffff;\n                border: 1px solid {_COLORS["border"]};\n                border-radius: 12px;\n            }}\n            QScrollArea > QWidget > QWidget {{\n                background-color: #ffffff;\n            }}\n        ')
        self.chat_messages_widget = QWidget()
        self.chat_messages_layout = QVBoxLayout(self.chat_messages_widget)
        self.chat_messages_layout.setContentsMargins(16, 16, 16, 16)
        self.chat_messages_layout.setSpacing(12)
        self.chat_messages_layout.addStretch()
        self.chat_display.setWidget(self.chat_messages_widget)
        chat_layout.addWidget(self.chat_display, 1)
        self._stick_to_bottom = True
        sb = self.chat_display.verticalScrollBar()
        sb.rangeChanged.connect(self._on_chat_range_changed)
        sb.valueChanged.connect(self._on_chat_value_changed)
        self.typing_indicator = QLabel('')
        self.typing_indicator.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 13px; padding: 4px 8px;')
        self.typing_indicator.hide()
        chat_layout.addWidget(self.typing_indicator)
        self.chips_area = QWidget()
        self.chips_layout = QHBoxLayout(self.chips_area)
        self.chips_layout.setContentsMargins(4, 0, 4, 4)
        self.chips_layout.setSpacing(6)
        self.chips_layout.addStretch()
        self.chips_area.hide()
        chat_layout.addWidget(self.chips_area)
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.chat_input = ChatInputBox()
        self.chat_input.send_requested.connect(self._on_send_message)
        self.attach_btn = QPushButton()
        self.attach_btn.setObjectName('attachBtn')
        self.attach_btn.setIcon(_make_paperclip_icon(20, _COLORS['text_dim']))
        self.attach_btn.setIconSize(QSize(20, 20))
        self.attach_btn.setFixedSize(42, 42)
        self.attach_btn.setToolTip(T('ui.attach'))
        self.attach_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.attach_btn.clicked.connect(self._on_attach_clicked)
        self.send_btn = QPushButton('➤')
        self.send_btn.setObjectName('sendBtn')
        self.send_btn.setFixedSize(48, 48)
        self.send_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.send_btn.clicked.connect(self._on_send_click)
        input_row.addWidget(self.chat_input)
        input_row.addWidget(self.attach_btn)
        input_row.addWidget(self.send_btn)
        chat_layout.addLayout(input_row)
        outer.addWidget(chat_area, 1)

    def _build_tab_module(self):
        layout = QHBoxLayout(self.tab_module)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(24)
        left_col = QVBoxLayout()
        left_col.setSpacing(12)
        self.status_title = QLabel(T('module.status_title'))
        self.status_title.setFont(QFont('Segoe UI', 14, QFont.Weight.Bold))
        left_col.addWidget(self.status_title)
        status_row = QHBoxLayout()
        self.status_dot = QLabel('●')
        self.status_dot.setFont(QFont('Segoe UI', 18))
        self.status_dot.setStyleSheet(f'color: {_COLORS["text_dim"]};')
        self.status_label = QLabel(T('ui.starting'))
        self.status_label.setFont(QFont('Segoe UI', 12, QFont.Weight.Bold))
        self.status_detail = QLabel('')
        self.status_detail.setStyleSheet(f'color: {_COLORS["text_dim"]};')
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label)
        status_row.addWidget(self.status_detail)
        status_row.addStretch()
        left_col.addLayout(status_row)
        left_col.addStretch()
        company_row = QHBoxLayout()
        company_row.setContentsMargins(0, 0, 0, 0)
        company_logo = QLabel()
        logo_path = self._find_logo('biomem_logo.svg')
        if logo_path:
            pixmap = QPixmap(str(logo_path)).scaledToHeight(65, Qt.TransformationMode.SmoothTransformation)
            company_logo.setPixmap(pixmap)
        else:
            company_logo.setText('biomem')
            company_logo.setFont(QFont('Segoe UI', 11, QFont.Weight.Bold))
            company_logo.setStyleSheet(f'color: {_COLORS["text"]};')
        company_logo.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        company_logo.mousePressEvent = lambda e: None
        company_row.addWidget(company_logo)
        company_row.addStretch()
        left_col.addLayout(company_row)
        layout.addLayout(left_col, 1)
        right_frame = QFrame()
        right_frame.setObjectName('rightPanel')
        panel_shadow = QGraphicsDropShadowEffect()
        panel_shadow.setBlurRadius(40)
        panel_shadow.setColor(QColor(0, 0, 0, 15))
        panel_shadow.setOffset(0, 10)
        right_frame.setGraphicsEffect(panel_shadow)
        right_layout = QVBoxLayout(right_frame)
        right_layout.setContentsMargins(20, 20, 20, 20)
        right_layout.setSpacing(16)
        ltm_layout = QVBoxLayout()
        self.ltm_lbl = QLabel('LTM: — / —')
        self.ltm_lbl.setFont(QFont('Segoe UI', 11, QFont.Weight.Bold))
        self.ltm_progress = QProgressBar()
        self.ltm_progress.setTextVisible(False)
        self.ltm_progress.setFixedHeight(6)
        ltm_layout.addWidget(self.ltm_lbl)
        ltm_layout.addWidget(self.ltm_progress)
        right_layout.addLayout(ltm_layout)
        stm_layout = QVBoxLayout()
        self.stm_lbl = QLabel('STM: — / —')
        self.stm_lbl.setFont(QFont('Segoe UI', 11, QFont.Weight.Bold))
        self.stm_progress = QProgressBar()
        self.stm_progress.setTextVisible(False)
        self.stm_progress.setFixedHeight(6)
        stm_layout.addWidget(self.stm_lbl)
        stm_layout.addWidget(self.stm_progress)
        right_layout.addLayout(stm_layout)
        self.stats_lbl = QLabel('Writes: — | Reads: —')
        self.stats_lbl.setStyleSheet(f'color: {_COLORS["text_dim"]};')
        right_layout.addWidget(self.stats_lbl)
        right_layout.addSpacing(8)
        btn_row = QHBoxLayout()
        self.backup_btn = QPushButton(T('module.backup'))
        self.backup_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.backup_btn.setMinimumHeight(38)
        self.backup_btn.clicked.connect(self._on_backup)
        self.restore_btn = QPushButton(T('module.restore'))
        self.restore_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.restore_btn.setMinimumHeight(38)
        self.restore_btn.clicked.connect(self._on_restore)
        btn_row.addWidget(self.backup_btn)
        btn_row.addWidget(self.restore_btn)
        right_layout.addLayout(btn_row)
        self.shutdown_hint = QLabel(T('module.shutdown_hint'))
        self.shutdown_hint.setWordWrap(True)
        self.shutdown_hint.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 11px; padding: 4px 2px;')
        right_layout.addWidget(self.shutdown_hint)
        self.shutdown_btn = QPushButton(T('module.shutdown'))
        self.shutdown_btn.setObjectName('dangerBtn')
        self.shutdown_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.shutdown_btn.setMinimumHeight(38)
        self.shutdown_btn.clicked.connect(self._on_quit_module)
        right_layout.addWidget(self.shutdown_btn)
        self.news_preview = QTextEdit()
        self.news_preview.setReadOnly(True)
        self.news_preview.setFixedHeight(100)
        self.news_preview.setPlaceholderText('The biomem memory web extension lets you use biomem directly on ChatGPT, Gemini and Claude websites.')
        right_layout.addWidget(self.news_preview)
        layout.addWidget(right_frame, 1)

    def _build_tab_memory(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('QScrollArea { background-color: transparent; } QWidget#memContainer { background-color: transparent; }')
        container = QWidget()
        container.setObjectName('memContainer')
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 16, 20, 16)
        layout.setSpacing(18)
        self.mem_title = QLabel(T('memory.title'))
        self.mem_title.setFont(QFont('Segoe UI', 16, QFont.Weight.Bold))
        layout.addWidget(self.mem_title)
        self.migration_frame = QFrame()
        self.migration_frame.setStyleSheet(f'background-color: {_COLORS["banner_bg"]}; border: 1px solid #fcd34d; border-radius: 12px;')
        mig_layout = QHBoxLayout(self.migration_frame)
        self.mig_lbl = QLabel(T('memory.legacy_import_hint'))
        self.mig_lbl.setStyleSheet(f'color: {_COLORS["banner_fg"]}; font-weight: bold;')
        self.mig_lbl.setWordWrap(True)
        self.mig_btn = QPushButton(T('memory.import_pt'))
        self.mig_btn.setObjectName('warningBtn')
        self.mig_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.mig_btn.clicked.connect(self._on_import_legacy_pt)
        mig_layout.addWidget(self.mig_lbl, 1)
        mig_layout.addWidget(self.mig_btn)
        layout.addWidget(self.migration_frame)
        self.migration_frame.hide()
        paap_card = QFrame()
        paap_card.setStyleSheet(f'\n            QFrame {{\n                background-color: {_COLORS["bg_panel"]};\n                border: 1px solid {_COLORS["accent"]};\n                border-radius: 12px;\n            }}\n        ')
        paap_layout = QVBoxLayout(paap_card)
        paap_layout.setContentsMargins(18, 16, 18, 16)
        paap_layout.setSpacing(12)
        paap_header = QHBoxLayout()
        self.paap_title = QLabel(T('memory.export_product'))
        self.paap_title.setFont(QFont('Segoe UI', 13, QFont.Weight.Bold))
        self.paap_title.setStyleSheet(f'color: {_COLORS["text"]}; border: none;')
        self.paap_badge = QLabel(T('memory.paap_badge'))
        self.paap_badge.setFont(QFont('Segoe UI', 10, QFont.Weight.Bold))
        self.paap_badge.setStyleSheet(f'color: {_COLORS["accent"]}; background-color: rgba(14, 165, 233, 0.12); padding: 4px 10px; border-radius: 6px; border: none;')
        paap_header.addWidget(self.paap_title)
        paap_header.addWidget(self.paap_badge)
        paap_header.addStretch()
        paap_layout.addLayout(paap_header)
        self.paap_desc = QLabel(T('memory.paap_desc'))
        self.paap_desc.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px; border: none; line-height: 1.4;')
        self.paap_desc.setWordWrap(True)
        paap_layout.addWidget(self.paap_desc)
        paap_btn_row = QHBoxLayout()
        paap_btn_row.setSpacing(12)
        self.export_product_btn = QPushButton(T('memory.export_product'))
        self.export_product_btn.setMinimumHeight(40)
        self.export_product_btn.setObjectName('actionBtn')
        self.export_product_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.export_product_btn.clicked.connect(self._on_export_product)
        paap_btn_row.addWidget(self.export_product_btn)
        self.export_report_btn = QPushButton(T('memory.export_report'))
        self.export_report_btn.setMinimumHeight(40)
        self.export_report_btn.setObjectName('actionBtn')
        self.export_report_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.export_report_btn.clicked.connect(self._on_export_report)
        paap_btn_row.addWidget(self.export_report_btn)
        paap_btn_row.addStretch()
        paap_layout.addLayout(paap_btn_row)
        layout.addWidget(paap_card)
        viz_card = QFrame()
        viz_card.setStyleSheet(f'\n            QFrame {{\n                background-color: {_COLORS["bg_panel"]};\n                border: 1px solid {_COLORS["border"]};\n                border-radius: 12px;\n            }}\n        ')
        viz_layout = QVBoxLayout(viz_card)
        viz_layout.setContentsMargins(18, 16, 18, 16)
        viz_layout.setSpacing(12)
        self.viz_title = QLabel(T('memory.card_viz_title'))
        self.viz_title.setFont(QFont('Segoe UI', 13, QFont.Weight.Bold))
        self.viz_title.setStyleSheet(f'color: {_COLORS["text"]}; border: none;')
        viz_layout.addWidget(self.viz_title)
        self.viz_desc = QLabel(T('memory.card_viz_desc'))
        self.viz_desc.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px; border: none;')
        self.viz_desc.setWordWrap(True)
        viz_layout.addWidget(self.viz_desc)
        chart_btn_layout = QHBoxLayout()
        chart_btn_layout.setSpacing(12)
        self.show_dendrogram_btn = QPushButton(T('memory.show_dendrogram'))
        self.show_dendrogram_btn.setMinimumHeight(40)
        self.show_dendrogram_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.show_dendrogram_btn.setObjectName('actionBtn')
        self.show_dendrogram_btn.clicked.connect(self._on_show_dendrogram)
        chart_btn_layout.addWidget(self.show_dendrogram_btn, 1)
        self.show_temporal_btn = QPushButton(T('memory.show_temporal'))
        self.show_temporal_btn.setMinimumHeight(40)
        self.show_temporal_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.show_temporal_btn.setObjectName('actionBtn')
        self.show_temporal_btn.clicked.connect(self._on_show_temporal_evolution)
        chart_btn_layout.addWidget(self.show_temporal_btn, 1)
        self.show_graph_btn = QPushButton(T('memory.show_graph'))
        self.show_graph_btn.setMinimumHeight(40)
        self.show_graph_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.show_graph_btn.setObjectName('actionBtn')
        self.show_graph_btn.clicked.connect(self._on_show_graph)
        chart_btn_layout.addWidget(self.show_graph_btn, 1)
        viz_layout.addLayout(chart_btn_layout)
        layout.addWidget(viz_card)
        params_card = QFrame()
        params_card.setStyleSheet(f'\n            QFrame {{\n                background-color: {_COLORS["bg_panel"]};\n                border: 1px solid {_COLORS["border"]};\n                border-radius: 12px;\n            }}\n        ')
        params_layout = QVBoxLayout(params_card)
        params_layout.setContentsMargins(18, 16, 18, 16)
        params_layout.setSpacing(14)
        params_header = QHBoxLayout()
        self.params_title = QLabel(T('memory.card_params_title'))
        self.params_title.setFont(QFont('Segoe UI', 13, QFont.Weight.Bold))
        self.params_title.setStyleSheet(f'color: {_COLORS["text"]}; border: none;')
        self.adv_warning = QLabel(T('memory.advanced_warning'))
        self.adv_warning.setStyleSheet(f'color: {_COLORS["warning"]}; font-size: 11px; font-weight: 600; border: none;')
        params_header.addWidget(self.params_title)
        params_header.addStretch()
        params_header.addWidget(self.adv_warning)
        params_layout.addLayout(params_header)
        assoc_row = QHBoxLayout()
        self.assoc_name_lbl = QLabel(T('memory.max_associations'))
        self.assoc_name_lbl.setStyleSheet(f'font-size: 13px; color: {_COLORS["text"]}; font-weight: 600; border: none;')
        self.assoc_name_lbl.setMinimumWidth(220)
        self._assoc_val_lbl = QLabel('5')
        self._assoc_val_lbl.setStyleSheet(f'font-size: 13px; color: {_COLORS["accent"]}; font-weight: 700; min-width: 36px; border: none;')
        assoc_row.addWidget(self.assoc_name_lbl)
        assoc_row.addStretch()
        assoc_row.addWidget(self._assoc_val_lbl)
        params_layout.addLayout(assoc_row)
        self._assoc_slider = QSlider(Qt.Orientation.Horizontal)
        self._assoc_slider.setRange(3, 10)
        self._assoc_slider.setSingleStep(1)
        self._assoc_slider.setPageStep(1)
        self._assoc_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._assoc_slider.setTickInterval(1)
        self._assoc_slider.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._assoc_slider.setStyleSheet('\n            QSlider::groove:horizontal {\n                height: 6px;\n                background: #e2e8f0;\n                border-radius: 3px;\n            }\n            QSlider::sub-page:horizontal {\n                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #38bdf8,stop:1 #0284c7);\n                border-radius: 3px;\n            }\n            QSlider::handle:horizontal {\n                background: #0ea5e9;\n                border: 2px solid #0284c7;\n                width: 16px;\n                height: 16px;\n                margin: -5px 0;\n                border-radius: 8px;\n            }\n            QSlider::handle:horizontal:hover {\n                background: #38bdf8;\n            }\n        ')
        self._assoc_slider.valueChanged.connect(lambda v: self._on_mem_threshold_changed('assoc', v))
        params_layout.addWidget(self._assoc_slider)
        params_layout.addSpacing(6)
        stm_row = QHBoxLayout()
        self.stm_name_lbl = QLabel(T('memory.stm_threshold'))
        self.stm_name_lbl.setStyleSheet(f'font-size: 13px; color: {_COLORS["text"]}; font-weight: 600; border: none;')
        self.stm_name_lbl.setMinimumWidth(220)
        self._stm_val_lbl = QLabel('0.00')
        self._stm_val_lbl.setStyleSheet(f'font-size: 13px; color: {_COLORS["accent"]}; font-weight: 700; min-width: 36px; border: none;')
        stm_row.addWidget(self.stm_name_lbl)
        stm_row.addStretch()
        stm_row.addWidget(self._stm_val_lbl)
        params_layout.addLayout(stm_row)
        self._stm_slider = QSlider(Qt.Orientation.Horizontal)
        self._stm_slider.setRange(25, 85)
        self._stm_slider.setSingleStep(1)
        self._stm_slider.setPageStep(5)
        self._stm_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._stm_slider.setTickInterval(5)
        self._stm_slider.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._stm_slider.setStyleSheet('\n            QSlider::groove:horizontal {\n                height: 6px;\n                background: #e2e8f0;\n                border-radius: 3px;\n            }\n            QSlider::sub-page:horizontal {\n                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #38bdf8,stop:1 #0284c7);\n                border-radius: 3px;\n            }\n            QSlider::handle:horizontal {\n                background: #0ea5e9;\n                border: 2px solid #0284c7;\n                width: 16px;\n                height: 16px;\n                margin: -5px 0;\n                border-radius: 8px;\n            }\n            QSlider::handle:horizontal:hover {\n                background: #38bdf8;\n            }\n        ')
        self._stm_slider.valueChanged.connect(lambda v: self._on_mem_threshold_changed('stm', v))
        params_layout.addWidget(self._stm_slider)
        params_layout.addSpacing(6)
        ltm_row = QHBoxLayout()
        self.ltm_name_lbl = QLabel(T('memory.ltm_threshold'))
        self.ltm_name_lbl.setStyleSheet(f'font-size: 13px; color: {_COLORS["text"]}; font-weight: 600; border: none;')
        self.ltm_name_lbl.setMinimumWidth(220)
        self._ltm_val_lbl = QLabel('0.00')
        self._ltm_val_lbl.setStyleSheet(f'font-size: 13px; color: {_COLORS["accent"]}; font-weight: 700; min-width: 36px; border: none;')
        ltm_row.addWidget(self.ltm_name_lbl)
        ltm_row.addStretch()
        ltm_row.addWidget(self._ltm_val_lbl)
        params_layout.addLayout(ltm_row)
        self._ltm_slider = QSlider(Qt.Orientation.Horizontal)
        self._ltm_slider.setRange(25, 85)
        self._ltm_slider.setSingleStep(1)
        self._ltm_slider.setPageStep(5)
        self._ltm_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._ltm_slider.setTickInterval(5)
        self._ltm_slider.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._ltm_slider.setStyleSheet('\n            QSlider::groove:horizontal {\n                height: 6px;\n                background: #e2e8f0;\n                border-radius: 3px;\n            }\n            QSlider::sub-page:horizontal {\n                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #38bdf8,stop:1 #0284c7);\n                border-radius: 3px;\n            }\n            QSlider::handle:horizontal {\n                background: #0ea5e9;\n                border: 2px solid #0284c7;\n                width: 16px;\n                height: 16px;\n                margin: -5px 0;\n                border-radius: 8px;\n            }\n            QSlider::handle:horizontal:hover {\n                background: #38bdf8;\n            }\n        ')
        self._ltm_slider.valueChanged.connect(lambda v: self._on_mem_threshold_changed('ltm', v))
        params_layout.addWidget(self._ltm_slider)
        params_layout.addSpacing(4)
        btn_row = QHBoxLayout()
        self.reset_defaults_btn = QPushButton(T('memory.reset_defaults'))
        self.reset_defaults_btn.setMinimumHeight(36)
        self.reset_defaults_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.reset_defaults_btn.setObjectName('actionBtn')
        self.reset_defaults_btn.clicked.connect(self._on_reset_mem_defaults)
        btn_row.addWidget(self.reset_defaults_btn)
        btn_row.addStretch()
        params_layout.addLayout(btn_row)
        effect_box = QFrame()
        effect_box.setStyleSheet('background-color: rgba(0,0,0,0.03); border-radius: 8px; padding: 4px; border: none;')
        effect_layout = QVBoxLayout(effect_box)
        effect_layout.setContentsMargins(10, 8, 10, 8)
        self.effect_lbl = QLabel(T('memory.effect_desc'))
        self.effect_lbl.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px; line-height: 1.5; border: none;')
        self.effect_lbl.setWordWrap(True)
        effect_layout.addWidget(self.effect_lbl)
        params_layout.addWidget(effect_box)
        layout.addWidget(params_card)
        maint_card = QFrame()
        maint_card.setStyleSheet(f'\n            QFrame {{\n                background-color: {_COLORS["bg_panel"]};\n                border: 1px solid {_COLORS["border"]};\n                border-radius: 12px;\n            }}\n        ')
        maint_layout = QVBoxLayout(maint_card)
        maint_layout.setContentsMargins(18, 16, 18, 16)
        maint_layout.setSpacing(14)
        self.maint_title = QLabel(T('memory.card_maint_title'))
        self.maint_title.setFont(QFont('Segoe UI', 13, QFont.Weight.Bold))
        self.maint_title.setStyleSheet(f'color: {_COLORS["text"]}; border: none;')
        maint_layout.addWidget(self.maint_title)
        self.maint_desc = QLabel(T('memory.card_maint_desc'))
        self.maint_desc.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px; border: none;')
        self.maint_desc.setWordWrap(True)
        maint_layout.addWidget(self.maint_desc)
        rebuild_row = QHBoxLayout()
        self.refactor_btn = QPushButton(T('memory.refactor'))
        self.refactor_btn.setObjectName('actionBtn')
        self.refactor_btn.setMinimumHeight(40)
        self.refactor_btn.setMinimumWidth(240)
        self.refactor_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.refactor_btn.clicked.connect(self._on_refactor_memory)
        rebuild_row.addWidget(self.refactor_btn)
        self.refactor_sub = QLabel(T('memory.refactor_subtitle'))
        self.refactor_sub.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px; border: none; margin-left: 8px;')
        self.refactor_sub.setWordWrap(True)
        rebuild_row.addWidget(self.refactor_sub, 1)
        maint_layout.addLayout(rebuild_row)
        maint_layout.addSpacing(4)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        divider.setStyleSheet(f'background-color: {_COLORS["border"]}; max-height: 1px; border: none;')
        maint_layout.addWidget(divider)
        self.clear_label = QLabel(T('memory.clear_subtitle'))
        self.clear_label.setFont(QFont('Segoe UI', 11, QFont.Weight.Bold))
        self.clear_label.setStyleSheet(f'color: {_COLORS["text"]}; border: none;')
        maint_layout.addWidget(self.clear_label)
        clear_btn_row = QHBoxLayout()
        self.clear_stm_btn = QPushButton(T('memory.clear_stm'))
        self.clear_stm_btn.setObjectName('warningBtn')
        self.clear_stm_btn.setMinimumHeight(40)
        self.clear_stm_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.clear_stm_btn.clicked.connect(self._on_clear_stm)
        self.clear_all_btn = QPushButton(T('memory.clear_all'))
        self.clear_all_btn.setObjectName('dangerBtn')
        self.clear_all_btn.setMinimumHeight(40)
        self.clear_all_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.clear_all_btn.clicked.connect(self._on_clear_all)
        clear_btn_row.addWidget(self.clear_stm_btn)
        clear_btn_row.addWidget(self.clear_all_btn)
        clear_btn_row.addStretch()
        maint_layout.addLayout(clear_btn_row)
        layout.addWidget(maint_card)
        layout.addStretch()
        scroll.setWidget(container)
        outer_layout = QVBoxLayout(self.tab_memory)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(scroll)

    def _build_tab_llm(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 16, 0, 16)
        layout.setSpacing(20)
        self.llm_title = QLabel(T('llm.title'))
        self.llm_title.setFont(QFont('Segoe UI', 16, QFont.Weight.Bold))
        layout.addWidget(self.llm_title)
        self.llm_subtitle = QLabel(T('llm.subtitle'))
        self.llm_subtitle.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 13px;')
        self.llm_subtitle.setWordWrap(True)
        layout.addWidget(self.llm_subtitle)
        self._llm_key_inputs = {}
        self._llm_model_inputs = {}
        self._llm_personal_inputs = {}
        self._llm_context_sliders = {}
        self._ollama_timeout_slider = None
        self._llm_group_widgets = {}
        MODELS = [
            ('chatgpt', 'ChatGPT (OpenAI)', 'https://platform.openai.com/api-keys', 'gpt-4o-mini'),
            ('gemini', 'Gemini (Google)', 'https://aistudio.google.com/app/apikey', 'gemini-2.5-flash'),
            ('claude', 'Claude (Anthropic)', 'https://console.anthropic.com/settings/keys', 'claude-sonnet-4-20250514'),
            ('ollama', f'Ollama ({T("llm.local")})', None, 'llama3'),
        ]
        for model_id, display_name, key_url, default_model in MODELS:
            group = self._build_llm_model_group(model_id, display_name, key_url, default_model)
            layout.addWidget(group)
        self.save_llm_btn = QPushButton(T('llm.save_settings'))
        self.save_llm_btn.setObjectName('actionBtn')
        self.save_llm_btn.setFixedHeight(42)
        self.save_llm_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.save_llm_btn.clicked.connect(self._on_save_llm_settings)
        layout.addWidget(self.save_llm_btn)
        layout.addStretch()
        scroll.setWidget(container)
        outer = QVBoxLayout(self.tab_llm)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self._load_llm_settings()

    def _build_llm_model_group(self, model_id: str, display_name: str, key_url: Optional[str], default_model: str) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f'\n            QFrame {{\n                background-color: {_COLORS["bg_panel"]};\n                border: 1px solid {_COLORS["border"]};\n                border-radius: 12px;\n            }}\n        ')
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        self._llm_group_widgets[model_id] = {}
        header = QHBoxLayout()
        name_lbl = QLabel(display_name)
        name_lbl.setFont(QFont('Segoe UI', 13, QFont.Weight.Bold))
        header.addWidget(name_lbl)
        header.addStretch()
        self._llm_group_widgets[model_id]['name_lbl'] = name_lbl
        self._llm_group_widgets[model_id]['display_name'] = display_name
        self._llm_group_widgets[model_id]['default_model'] = default_model
        if key_url:
            link_lbl = QLabel(f'<a href="{key_url}" style="color: {_COLORS["accent"]}; font-size: 12px;">{T("llm.get_api_key")}</a>')
            link_lbl.setOpenExternalLinks(True)
            header.addWidget(link_lbl)
            self._llm_group_widgets[model_id]['link_lbl'] = link_lbl
            self._llm_group_widgets[model_id]['link_url'] = key_url
        layout.addLayout(header)
        form = QFormLayout()
        form.setSpacing(8)
        key_row = QHBoxLayout()
        key_input = QLineEdit()
        if model_id == 'ollama':
            key_input.setPlaceholderText('http://localhost:11434')
        else:
            key_input.setEchoMode(QLineEdit.EchoMode.Password)
            key_input.setPlaceholderText(T('llm.placeholder_key', display_name.split()[0]))
        self._llm_key_inputs[model_id] = key_input
        self._llm_group_widgets[model_id]['key_input'] = key_input
        eye_btn = QPushButton('👁')
        eye_btn.setObjectName('clearBtn')
        eye_btn.setFixedSize(36, 36)
        eye_btn.setCheckable(True)
        eye_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        if model_id != 'ollama':
            def _toggle_visibility(checked, inp=key_input):
                if checked:
                    inp.setEchoMode(QLineEdit.EchoMode.Normal)
                else:
                    inp.setEchoMode(QLineEdit.EchoMode.Password)
            eye_btn.toggled.connect(_toggle_visibility)
        else:
            eye_btn.hide()
        key_row.addWidget(key_input)
        key_row.addWidget(eye_btn)
        key_label_text = T('llm.ollama_url') if model_id == 'ollama' else T('llm.api_key')
        key_label = QLabel(key_label_text)
        self._llm_group_widgets[model_id]['key_label'] = key_label
        form.addRow(key_label, key_row)
        model_input = QLineEdit()
        model_input.setPlaceholderText(f'{T("ui.default")}: {default_model}')
        self._llm_model_inputs[model_id] = model_input
        self._llm_group_widgets[model_id]['model_input'] = model_input
        model_label = QLabel(T('llm.model_name'))
        self._llm_group_widgets[model_id]['model_label'] = model_label
        form.addRow(model_label, model_input)
        ctx_row = QHBoxLayout()
        ctx_slider = QSlider(Qt.Orientation.Horizontal)
        ctx_slider.setMinimum(0)
        ctx_slider.setMaximum(2000)
        ctx_slider.setValue(250)
        ctx_slider.setFixedHeight(22)
        self._llm_context_sliders[model_id] = ctx_slider
        def _ctx_label_text(v: int) -> str:
            return T('llm.disabled') if v == 0 else f'{v} {T("llm.words")}'
        ctx_val_lbl = QLabel(_ctx_label_text(ctx_slider.value()))
        ctx_val_lbl.setFixedWidth(70)
        ctx_val_lbl.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px;')
        self._llm_group_widgets[model_id]['ctx_val_lbl'] = ctx_val_lbl
        def _update_ctx_label(v, lbl=ctx_val_lbl):
            lbl.setText(_ctx_label_text(v))
        ctx_slider.valueChanged.connect(_update_ctx_label)
        ctx_row.addWidget(ctx_slider)
        ctx_row.addWidget(ctx_val_lbl)
        context_label = QLabel(T('llm.context_window'))
        self._llm_group_widgets[model_id]['context_label'] = context_label
        form.addRow(context_label, ctx_row)
        if model_id == 'ollama':
            to_row = QHBoxLayout()
            to_slider = QSlider(Qt.Orientation.Horizontal)
            to_slider.setMinimum(7)
            to_slider.setMaximum(60)
            to_slider.setValue(7)
            to_slider.setFixedHeight(22)
            self._ollama_timeout_slider = to_slider
            to_val_lbl = QLabel(f'{to_slider.value()} {T("llm.min")}')
            to_val_lbl.setFixedWidth(70)
            to_val_lbl.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px;')
            self._llm_group_widgets[model_id]['to_val_lbl'] = to_val_lbl
            def _update_to_label(v, lbl=to_val_lbl):
                lbl.setText(f'{v} {T("llm.min")}')
            to_slider.valueChanged.connect(_update_to_label)
            to_row.addWidget(to_slider)
            to_row.addWidget(to_val_lbl)
            timeout_label = QLabel(T('llm.request_timeout'))
            self._llm_group_widgets[model_id]['timeout_label'] = timeout_label
            form.addRow(timeout_label, to_row)
        layout.addLayout(form)
        personal_label = QLabel(T('llm.system_prompt'))
        personal_label.setStyleSheet(f'color: {_COLORS["text_dim"]}; font-size: 12px; font-weight: 600;')
        self._llm_group_widgets[model_id]['personal_label'] = personal_label
        layout.addWidget(personal_label)
        personal_input = QTextEdit()
        personal_input.setFixedHeight(72)
        personal_input.setPlaceholderText(T('llm.placeholder_personal'))
        personal_input.setStyleSheet(f'\n            QTextEdit {{\n                background-color: {_COLORS["bg_input"]};\n                border: 1px solid {_COLORS["border"]};\n                border-radius: 8px;\n                padding: 8px 12px;\n                font-size: 13px;\n                color: {_COLORS["text"]};\n            }}\n        ')
        self._llm_personal_inputs[model_id] = personal_input
        self._llm_group_widgets[model_id]['personal_input'] = personal_input
        layout.addWidget(personal_input)
        return frame

    def _build_tab_news(self):
        layout = QVBoxLayout(self.tab_news)
        layout.setContentsMargins(0, 20, 0, 0)
        self.news_title = QLabel(T('news.title'))
        self.news_title.setFont(QFont('Segoe UI', 16, QFont.Weight.Bold))
        layout.addWidget(self.news_title)
        self.news_text = QTextEdit()
        self.news_text.setReadOnly(True)
        self.news_text.setStyleSheet(f'\n            QTextEdit {{\n                background-color: {_COLORS["bg_input"]};\n                border-radius: 12px;\n                padding: 16px;\n                font-size: 15px;\n                color: {_COLORS["text_white"]};\n                line-height: 1.6;\n            }}\n        ')
        layout.addWidget(self.news_text)

    def set_conversation_handler(self, handler):
        """Called from main thread when ConversationHandler is ready."""
        self._conv_handler = handler
        self._connect_conv_handler(handler)
        self._refresh_thread_list()
        QTimer.singleShot(1500, self._maybe_show_first_run)

    def _connect_conv_handler(self, handler):
        handler.user_message.connect(self._chat_add_user_message)
        handler.model_message.connect(self._chat_add_model_message)
        handler.thinking_update.connect(self._chat_show_typing)
        handler.thinking_hide.connect(self._chat_hide_typing)
        handler.thinking_bubble.connect(self._chat_add_thinking_bubble)
        handler.system_message.connect(self._chat_add_system_message)
        handler.pam_token_warning.connect(self._chat_add_pam_warning)
        handler.history_updated.connect(self._on_history_updated)
        handler.busy_changed.connect(self._on_busy_changed)
        handler.first_run.connect(self._show_first_run_wizard)

    _BUBBLE_MAX_WIDTH = 720
    _MAX_VISIBLE_BUBBLES = 200

    def _chat_clear_messages(self):
        """Clears all messages from the scroll area, keeping only the trailing stretch."""
        layout = self.chat_messages_layout
        while layout.count() > 0:
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        layout.addStretch()

    def _on_chat_range_changed(self, _min: int, _max: int):
        if self._stick_to_bottom:
            self.chat_display.verticalScrollBar().setValue(_max)

    def _on_chat_value_changed(self, value: int):
        sb = self.chat_display.verticalScrollBar()
        self._stick_to_bottom = value >= sb.maximum() - 4

    def _chat_insert_row(self, bubble: QWidget, align: str = 'left'):
        """Inserts a bubble as a row into the chat layout (before the trailing stretch)."""
        row = QWidget()
        row.setStyleSheet('background: transparent;')
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        if align == 'right':
            h.addStretch()
            h.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight)
        elif align == 'center':
            h.addStretch()
            h.addWidget(bubble, 0, Qt.AlignmentFlag.AlignCenter)
            h.addStretch()
        else:
            h.addWidget(bubble, 0, Qt.AlignmentFlag.AlignLeft)
            h.addStretch()
        insert_at = max(0, self.chat_messages_layout.count() - 1)
        self._stick_to_bottom = True
        self.chat_messages_layout.insertWidget(insert_at, row)
        layout = self.chat_messages_layout
        while layout.count() - 1 > self._MAX_VISIBLE_BUBBLES:
            item = layout.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()

    def _make_attachment_badge(self, name: str) -> QLabel:
        ext = Path(name).suffix.lower()
        icon = '🖼' if ext in {'.bmp', '.gif', '.jpg', '.png', '.jpeg', '.webp'} else '📄'
        badge = QLabel(f'{icon} {name}')
        badge.setStyleSheet('background:#e0f2fe; border:1px solid #bae6fd; border-radius:10px; padding:2px 8px; font-size:11px; color:#0369a1;')
        return badge

    def _chat_add_user_message(self, text: str, attach_names: list = None):
        bubble = QFrame()
        bubble.setObjectName('userBubble')
        bubble.setStyleSheet(f'\n            QFrame#userBubble {{\n                background-color: {_COLORS["chat_user_bg"]};\n                border-top-left-radius: 14px;\n                border-top-right-radius: 14px;\n                border-bottom-left-radius: 14px;\n                border-bottom-right-radius: 4px;\n            }}\n        ')
        bubble.setMaximumWidth(self._BUBBLE_MAX_WIDTH)
        v = QVBoxLayout(bubble)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(6)
        if attach_names:
            badges_row = QWidget()
            br = QHBoxLayout(badges_row)
            br.setContentsMargins(0, 0, 0, 0)
            br.setSpacing(4)
            br.addStretch()
            for name in attach_names:
                br.addWidget(self._make_attachment_badge(name))
            v.addWidget(badges_row)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet(f'color: {_COLORS["chat_user_fg"]}; font-size: 14px; background: transparent; border: none;')
        v.addWidget(label)
        self._chat_insert_row(bubble, align='right')

    def _chat_add_model_message(self, text: str):
        bubble = QFrame()
        bubble.setObjectName('modelBubble')
        bubble.setStyleSheet(f'\n            QFrame#modelBubble {{\n                background-color: {_COLORS["chat_model_bg"]};\n                border-top-left-radius: 4px;\n                border-top-right-radius: 14px;\n                border-bottom-left-radius: 14px;\n                border-bottom-right-radius: 14px;\n            }}\n        ')
        bubble.setMaximumWidth(self._BUBBLE_MAX_WIDTH)
        v = QVBoxLayout(bubble)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(0)
        label = QLabel(self._markdown_to_html(text))
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        label.setStyleSheet(f'color: {_COLORS["chat_model_fg"]}; font-size: 14px; background: transparent; border: none;')
        v.addWidget(label)
        self._chat_insert_row(bubble, align='left')

    def _chat_add_thinking_bubble(self, text: str):
        bubble = QFrame()
        bubble.setObjectName('thinkingBubble')
        bubble.setStyleSheet('\n            QFrame#thinkingBubble {\n                background-color: #f0fdf4;\n                border: 1px solid #bbf7d0;\n                border-radius: 8px;\n            }\n        ')
        bubble.setMaximumWidth(self._BUBBLE_MAX_WIDTH)
        v = QVBoxLayout(bubble)
        v.setContentsMargins(12, 6, 12, 6)
        v.setSpacing(0)
        label = QLabel(f'💭 {text}')
        label.setWordWrap(True)
        label.setStyleSheet('color: #166534; font-size: 13px; font-style: italic; background: transparent; border: none;')
        v.addWidget(label)
        self._chat_insert_row(bubble, align='left')

    def _chat_add_system_message(self, text: str, level: str = 'info'):
        color = {'error': '#ef4444', 'warning': '#f59e0b', 'info': '#64748b'}.get(level, '#64748b')
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(f'color: {color}; font-size: 12px; background: transparent; border: none;')
        self._chat_insert_row(label, align='center')

    def _on_thinking_tick(self):
        """Rotate through thinking phrases every 1.2 seconds."""
        self._thinking_index = (self._thinking_index + 1) % len(self._thinking_phrases)
        phrase = self._thinking_phrases[self._thinking_index]
        self.typing_indicator.setText(f'● {phrase}...')

    def _chat_show_typing(self, text: str = ''):
        if text and not text.startswith('Thinking'):
            self._thinking_timer.stop()
            self.typing_indicator.setText(f'● {text}')
        else:
            import random as _random
            self._thinking_index = _random.randrange(len(self._thinking_phrases))
            phrase = self._thinking_phrases[self._thinking_index]
            self.typing_indicator.setText(f'● {phrase}...')
            self._thinking_timer.start()
        self.typing_indicator.show()

    def _chat_hide_typing(self):
        self._thinking_timer.stop()
        self.typing_indicator.hide()

    def _chat_add_pam_warning(self):
        """Show a yellow warning message below an empty model bubble."""
        frame = QFrame()
        frame.setStyleSheet('QFrame { background: #fef9c3; border: 1px solid #fde047; border-radius: 8px; }')
        frame.setMaximumWidth(self._BUBBLE_MAX_WIDTH)
        v = QVBoxLayout(frame)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(4)
        lbl = QLabel(T('msg.pam_warning_html'))
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setStyleSheet('color: #713f12; font-size: 13px; background: transparent; border: none;')
        v.addWidget(lbl)
        self._chat_insert_row(frame, align='left')

    def _on_busy_changed(self, busy: bool):
        enabled = not busy
        self.chat_input.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)
        self.attach_btn.setEnabled(enabled)
        self.deep_recall_toggle.setEnabled(enabled)
        self.web_search_toggle.setEnabled(enabled)
        if not busy:
            self.chat_input.setFocus()

    def _get_selected_model(self) -> str:
        mapping = {'chatgpt': 0, 'gemini': 1, 'claude': 2, 'ollama': 3}
        return mapping.get(self.model_combo.currentIndex(), 'chatgpt')

    def _on_attach_clicked(self):
        exts = (
            f"{T('ui.filter_all')} (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.pdf *.txt *.md *.csv);;"
            f"{T('ui.filter_images')} (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;"
            f"{T('ui.filter_pdf')} (*.pdf);;"
            f"{T('ui.filter_text')} (*.txt *.md *.csv)"
        )
        paths, _ = QFileDialog.getOpenFileNames(self, T('ui.attach'), '', exts)
        for p in paths:
            if p not in self._pending_attachments:
                self._pending_attachments.append(p)
        self._rebuild_chips()

    def _rebuild_chips(self):
        while self.chips_layout.count() > 1:
            item = self.chips_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for i, path in enumerate(self._pending_attachments):
            name = Path(path).name
            ext = Path(path).suffix.lower()
            icon = '🖼' if ext in {'.bmp', '.gif', '.jpg', '.png', '.jpeg', '.webp'} else '📄'
            chip = QFrame()
            chip.setStyleSheet('QFrame { background:#e0f2fe; border:1px solid #bae6fd; border-radius:12px; padding:2px 4px; }')
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(8, 3, 4, 3)
            cl.setSpacing(4)
            lbl = QLabel(f'{icon} {name}')
            lbl.setStyleSheet('font-size:12px; color:#0369a1; background:transparent; border:none;')
            rm = QPushButton('✕')
            rm.setFixedSize(16, 16)
            rm.setStyleSheet('QPushButton { background:transparent; border:none; color:#64748b; font-size:10px; padding:0; }QPushButton:hover { color:#ef4444; }')
            rm.clicked.connect(lambda _, ix=i: self._remove_attachment(ix))
            cl.addWidget(lbl)
            cl.addWidget(rm)
            self.chips_layout.insertWidget(self.chips_layout.count() - 1, chip)
        self.chips_area.setVisible(bool(self._pending_attachments))

    def _remove_attachment(self, idx: int):
        if 0 <= idx < len(self._pending_attachments):
            self._pending_attachments.pop(idx)
        self._rebuild_chips()

    def _clear_attachments(self):
        self._pending_attachments.clear()
        self._rebuild_chips()

    def _on_send_message(self, text: str):
        if not text and not self._pending_attachments:
            return
        if not self._conv_handler:
            self._chat_add_system_message(T('msg.not_ready_err'), 'error')
            return
        attachments = list(self._pending_attachments)
        self._clear_attachments()
        model = self._get_selected_model()
        mode = 'deep' if self.deep_recall_toggle.isChecked() else 'associative'
        use_web = self.web_search_toggle.isChecked()
        self._conv_handler.process_message(text, model, mode, use_web, attachments)

    def _on_send_click(self):
        text = self.chat_input.toPlainText().strip()
        if text or self._pending_attachments:
            self.chat_input.clear()
            self.chat_input.setFixedHeight(56)
            self._on_send_message(text)


    def _on_new_chat(self):
        if not self._conv_handler:
            return
        self._clear_attachments()
        self._conv_handler.create_new_thread()
        self._chat_clear_messages()
        self._chat_add_system_message(T('msg.new_chat_started'), 'info')

    def _on_thread_selected(self, item: QListWidgetItem):
        if not self._conv_handler:
            return
        thread_id = item.data(Qt.ItemDataRole.UserRole)
        if not thread_id:
            return
        self._clear_attachments()
        self._conv_handler.switch_thread(thread_id)
        self._chat_clear_messages()
        history = self._conv_handler.get_current_history()
        if len(history) > self._MAX_VISIBLE_BUBBLES:
            history = history[-self._MAX_VISIBLE_BUBBLES:]
        for msg in history:
            if msg['role'] == 'user':
                self._chat_add_user_message(msg['text'])
            else:
                self._chat_add_model_message(msg['text'])

    def _on_thread_context_menu(self, pos):
        from PyQt6.QtWidgets import QMenu
        item = self.thread_list.itemAt(pos)
        if not item:
            return
        thread_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        rename_act = menu.addAction(T('ui.rename'))
        delete_act = menu.addAction(T('ui.delete'))
        action = menu.exec(self.thread_list.mapToGlobal(pos))
        if action == rename_act and self._conv_handler:
            new_name, ok = self._simple_input_dialog(T('msg.rename_thread_title'), T('msg.new_name'), item.text())
            if ok and new_name.strip():
                self._conv_handler.rename_thread(thread_id, new_name.strip())
            return
        if action == delete_act and self._conv_handler:
            if QMessageBox.question(
                self,
                T('msg.delete_thread_title'),
                T('msg.delete_thread_confirm'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes:
                self._conv_handler.delete_thread(thread_id)

    @staticmethod
    def _simple_input_dialog(title: str, label: str, default: str = '') -> tuple:
        from PyQt6.QtWidgets import QInputDialog
        return QInputDialog.getText(None, title, label, text=default)

    def _on_history_updated(self, threads: list):
        self._refresh_thread_list(threads)

    def _refresh_thread_list(self, threads: Optional[List[Dict]] = None):
        if threads is None and self._conv_handler:
            threads = self._conv_handler.get_thread_list()
        if threads is None:
            return
        self.thread_list.clear()
        for t in threads:
            item = QListWidgetItem(t.get('title', T('ui.new_chat')))
            item.setData(Qt.ItemDataRole.UserRole, t['id'])
            self.thread_list.addItem(item)

    def _maybe_show_first_run(self):
        # NOTE: body is an empty stub (only
        # LOAD_CONST None / RETURN_VALUE); emitted as `pass`.
        pass

    def _show_first_run_wizard(self):
        dlg = QDialog(self)
        dlg.setWindowTitle(T('wizard.title'))
        dlg.setMinimumWidth(480)
        dlg.setStyleSheet(
            f'\n            QDialog {{\n                background-color: {_COLORS["bg_card"]};\n            }}\n            QLabel {{\n                color: {_COLORS["text_white"]};\n            }}\n        '
        )
        layout = QVBoxLayout(dlg)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)
        welcome_lbl = QLabel(T('wizard.welcome'))
        welcome_lbl.setWordWrap(True)
        welcome_lbl.setStyleSheet(f'font-size: 15px; color: {_COLORS["text"]};')
        layout.addWidget(welcome_lbl)
        form = QFormLayout()
        form.setSpacing(10)
        name_input = QLineEdit()
        name_input.setPlaceholderText(T('wizard.name_placeholder'))
        form.addRow(QLabel(T('wizard.name')), name_input)
        profession_input = QLineEdit()
        profession_input.setPlaceholderText(T('wizard.profession_placeholder'))
        form.addRow(QLabel(T('wizard.profession')), profession_input)
        goal_input = QLineEdit()
        goal_input.setPlaceholderText(T('wizard.goal_placeholder'))
        form.addRow(QLabel(T('wizard.goal')), goal_input)
        layout.addLayout(form)
        import_lbl = QLabel(T('wizard.import_title'))
        import_lbl.setStyleSheet(f'font-size: 13px; color: {_COLORS["text_dim"]};')
        layout.addWidget(import_lbl)
        import_row = QHBoxLayout()
        self._import_path_lbl = QLabel(T('wizard.no_file'))
        self._import_path_lbl.setStyleSheet(f'font-size: 12px; color: {_COLORS["text_dim"]};')
        import_btn = QPushButton(T('wizard.browse'))
        import_btn.setFixedWidth(80)
        import_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._import_file_path = None

        def _browse():
            p, _ = QFileDialog.getOpenFileName(
                dlg,
                T('ui.select_history'),
                '',
                f"{T('ui.text_files')} (*.txt);;{T('ui.all_files')} (*.*)",
            )
            if p:
                self._import_file_path = p
                self._import_path_lbl.setText(Path(p).name)

        import_btn.clicked.connect(_browse)
        import_row.addWidget(self._import_path_lbl)
        import_row.addWidget(import_btn)
        layout.addLayout(import_row)
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        layout.addWidget(btn_box)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name = name_input.text().strip()
            profession = profession_input.text().strip()
            goal = goal_input.text().strip()
            if name or profession or goal:
                lines = []
                if name:
                    lines.append(f'My name is {name}.')
                if profession:
                    lines.append(f'I work as a {profession}.')
                if goal:
                    lines.append(f'My main goal is: {goal}.')
                seed_text = ' '.join(lines)
                for model_id in ('chatgpt', 'gemini', 'claude', 'ollama'):
                    existing = self._settings.get_personalisation(model_id)
                    if not existing:
                        self._settings.set_personalisation(model_id, seed_text)
                if self._conv_handler and self._conv_handler._handler:
                    try:
                        self._conv_handler._handler.memory.store(key='About me', value=seed_text)
                        self._conv_handler._handler.memory.save()
                    except Exception as e:
                        logger.warning(f'First-run memory store failed: {e}')
            self._load_llm_settings()
            if self._import_file_path and self._conv_handler and self._conv_handler._handler:
                self._do_batch_import(self._import_file_path)

    def _do_batch_import(self, file_path: str):
        """Batch import a .txt history file into biomem memory."""
        try:
            text = Path(file_path).read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            QMessageBox.warning(self, T('ui.error'), T('msg.import_failed_read', str(e)))
            return
        pairs = []
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        i = 0
        while i < len(lines) - 1:
            l = lines[i]
            n = lines[i + 1]
            user_prefixes = ('User:', 'Human:', 'You:')
            model_prefixes = ('Model:', 'Assistant:', 'AI:', 'Bot:')
            u_stripped = None
            m_stripped = None
            for p in user_prefixes:
                if l.startswith(p):
                    u_stripped = l[len(p):].strip()
            for p in model_prefixes:
                if n.startswith(p):
                    m_stripped = n[len(p):].strip()
            if u_stripped and m_stripped:
                pairs.append({'user': u_stripped, 'model': m_stripped})
                i += 2
            else:
                i += 1
        if not pairs:
            QMessageBox.information(self, T('module.restore'), T('msg.import_no_pairs'))
            return
        if not self._async_loop:
            return

        async def _import():
            handler = self._conv_handler._handler
            for pair in pairs:
                try:
                    handler.memory.store(key=pair['user'], value=pair['model'], intensity=0.6)
                except Exception:
                    pass
            try:
                handler.memory.consolidate()
                handler.memory.save()
            except Exception:
                return

        self._async_loop.call_soon_threadsafe(asyncio.ensure_future, _import())
        QMessageBox.information(self, T('module.restore'), T('msg.import_started', len(pairs)))

    def _load_llm_settings(self):
        if not self._settings:
            return
        for model_id, inp in self._llm_key_inputs.items():
            inp.setText(self._settings.get_llm_key(model_id))
        for model_id, inp in self._llm_model_inputs.items():
            inp.setText(self._settings.get_llm_model_name(model_id))
        for model_id, inp in self._llm_personal_inputs.items():
            inp.setPlainText(self._settings.get_personalisation(model_id))
        for model_id, slider in self._llm_context_sliders.items():
            slider.setValue(self._settings.get_context_limit(model_id))
        if self._ollama_timeout_slider is not None:
            self._ollama_timeout_slider.setValue(self._settings.get_ollama_timeout_min())

    def _on_save_llm_settings(self):
        if not self._settings:
            return
        for model_id, inp in self._llm_key_inputs.items():
            self._settings.set_llm_key(model_id, inp.text())
        for model_id, inp in self._llm_model_inputs.items():
            self._settings.set_llm_model_name(model_id, inp.text())
        for model_id, inp in self._llm_personal_inputs.items():
            self._settings.set_personalisation(model_id, inp.toPlainText())
        for model_id, slider in self._llm_context_sliders.items():
            self._settings.set_context_limit(model_id, slider.value())
        if self._ollama_timeout_slider is not None:
            self._settings.set_ollama_timeout_min(self._ollama_timeout_slider.value())
        QMessageBox.information(self, T('llm.title'), T('msg.save_success'))

    def post_message(
        self,
        msg_type: str,
        data: Optional[DashboardMessagePayload] = None,
    ) -> None:
        """Queues one structured payload for processing on the Qt main thread."""
        self.signals.message_received.emit(msg_type, dict(data) if data else {})

    def _process_message(self, msg_type: str, data: dict):
        try:
            self._process_message_inner(msg_type, data)
        except Exception:
            logger.exception('_process_message error (type=%s)', msg_type)

    def _process_message_inner(self, msg_type: str, data: dict):
        if msg_type == MSG_SERVER_READY:
            self._server_ready = True
            self._set_status(T('ui.ready'), '', _COLORS['success'])
            return
        if msg_type == MSG_MEMORY_STATS:
            self._update_memory_stats(data)
            return
        if msg_type == MSG_STATUS_UPDATE:
            self._set_status(data.get('text', ''), data.get('detail', ''), data.get('color', _COLORS['text']))
            return
        if msg_type == MSG_BACKUP_DONE:
            if data.get('status') == 'error':
                QMessageBox.warning(self, T('module.backup'), data.get('error', T('ui.error')))
                return
            QMessageBox.information(self, T('module.backup'), T('msg.backup_done', data.get('path', '?')))
            return
        if msg_type == MSG_RESTORE_DONE:
            if data.get('status') == 'error':
                QMessageBox.warning(self, T('module.restore'), data.get('error', T('ui.error')))
                return
            QMessageBox.information(self, T('module.restore'), T('msg.restore_done'))
            return
        if msg_type == MSG_EXPORT_DONE:
            if data.get('status') == 'error':
                QMessageBox.warning(self, T('memory.export_product'), data.get('error', T('ui.error')))
                return
            QMessageBox.information(self, T('memory.export_product'), T('msg.export_done', data.get('path', '?')))
            return
        if msg_type == MSG_REPORT_DONE:
            if data.get('status') == 'error':
                QMessageBox.warning(
                    self,
                    T('memory.export_report'),
                    T('msg.report_failed', data.get('error', T('ui.error'))),
                )
                return
            QMessageBox.information(self, T('memory.export_report'), T('msg.report_success', data.get('path', '?')))
            return
        if msg_type == MSG_NEWS_LOADED:
            self._display_news(data.get('content', ''), data.get('news_id', ''))
            return
        if msg_type == MSG_CONV_HANDLER_READY:
            self._refresh_thread_list()
            QTimer.singleShot(1500, self._maybe_show_first_run)
            return
        if msg_type == MSG_REFACTOR_DONE:
            if hasattr(self, '_refactor_dlg') and self._refactor_dlg is not None:
                try:
                    self._refactor_dlg.show_result(data)
                except RuntimeError:
                    pass
                self._refactor_dlg = None
            return
        if msg_type == MSG_REFACTOR_PROGRESS:
            if hasattr(self, '_refactor_dlg') and self._refactor_dlg is not None:
                try:
                    self._refactor_dlg.update_progress(
                        data.get('step', ''),
                        data.get('current', 0),
                        data.get('total', 0),
                        data.get('detail', ''),
                    )
                except RuntimeError:
                    return
            return
        if msg_type == '__quit__':
            self._on_quit_module()
            return
        if msg_type == '__show__':
            self.show()
            self.raise_()
            self.activateWindow()

    def _set_status(self, text: str, detail: str = '', color: str = None):
        clean = text.lstrip('✅❌⏳ ')
        self.status_label.setText(clean)
        self.status_detail.setText(detail)
        c = color if color else _COLORS['text_dim']
        self.status_label.setStyleSheet(f'color: {c};')
        self.status_dot.setStyleSheet(f'color: {c};')
        self.chat_status_lbl.setText(clean)
        self.chat_status_dot.setStyleSheet(f'color: {c}; font-size: 14px;')
        if c == _COLORS['success']:
            glow = QGraphicsDropShadowEffect()
            glow.setBlurRadius(10)
            glow.setColor(QColor(c))
            glow.setOffset(0, 0)
            self.status_dot.setGraphicsEffect(glow)
            return
        self.status_dot.setGraphicsEffect(None)

    def _update_memory_stats(self, stats: Dict):
        self._memory_stats = stats
        ltm_active = stats.get('ltm_active', 0)
        ltm_total = stats.get('ltm_total', 1)
        stm_active = stats.get('stm_active', 0)
        stm_total = stats.get('stm_total', 1)
        writes = stats.get('writes', 0)
        reads = stats.get('reads', 0)
        fatigue = stats.get('fatigue_pct', 0)
        self.ltm_lbl.setText(f'LTM: {ltm_active} / {ltm_total}')
        self.ltm_progress.setMaximum(ltm_total)
        self.ltm_progress.setValue(ltm_active)
        self.stm_lbl.setText(f'STM: {stm_active} / {stm_total}')
        self.stm_progress.setMaximum(stm_total)
        self.stm_progress.setValue(stm_active)
        self.stats_lbl.setText(f"{T('ui.writes')}: {writes}  |  {T('ui.reads')}: {reads}")
        self.chat_ltm_lbl.setText(f'LTM: {ltm_active} / {ltm_total}')
        self.chat_stm_lbl.setText(f'STM: {stm_active} / {stm_total}')
        self.chat_fatigue_lbl.setText(f"{T('ui.fatigue_label')}: {fatigue:.1f}%")
        locked = bool(self._settings and getattr(self._settings, 'pt_import_locked', False))
        if writes == 0 and not locked:
            self.migration_frame.show()
            return
        self.migration_frame.hide()

    def _load_settings(self):
        self._set_status(T('ui.starting'), '', _COLORS['text_dim'])
        self._load_mem_thresholds()

    def _display_news(self, content: str, news_id: str):
        import re

        def md_to_html(text):
            text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
            lines = text.split('\n')
            out = []
            for line in lines:
                if line.startswith('### '):
                    out.append(f'<h4>{line[4:]}</h4>')
                elif line.startswith('## '):
                    out.append(f'<h3>{line[3:]}</h3>')
                elif line.startswith('# '):
                    out.append(f'<h2>{line[2:]}</h2>')
                elif line.startswith('- '):
                    out.append(f'&nbsp;&nbsp;• {line[2:]}')
                else:
                    out.append(line)
            return '<br>'.join(out)

        html_content = md_to_html(content)
        self.news_text.setHtml(html_content)
        lines = content.split('\n')
        preview_text = '\n'.join(lines[:6])
        self.news_preview.setHtml(md_to_html(preview_text))
        if news_id:
            self._settings.set_last_news_id(news_id)

    def _load_mem_thresholds(self):
        """Loads saved threshold values from SettingsManager and sets sliders."""
        if not (self._settings and hasattr(self, '_stm_slider')):
            return
        stm_v = self._settings.get_stm_threshold()
        ltm_v = self._settings.get_ltm_threshold()
        assoc_v = self._settings.get_max_associations()
        self._stm_slider.blockSignals(True)
        self._ltm_slider.blockSignals(True)
        if hasattr(self, '_assoc_slider'):
            self._assoc_slider.blockSignals(True)
        self._stm_slider.setValue(round(stm_v * 100))
        self._ltm_slider.setValue(round(ltm_v * 100))
        if hasattr(self, '_assoc_slider'):
            self._assoc_slider.setValue(assoc_v)
            self._assoc_val_lbl.setText(str(assoc_v))
        self._stm_val_lbl.setText(f'{stm_v:.2f}')
        self._ltm_val_lbl.setText(f'{ltm_v:.2f}')
        self._stm_slider.blockSignals(False)
        self._ltm_slider.blockSignals(False)
        if hasattr(self, '_assoc_slider'):
            self._assoc_slider.blockSignals(False)

    def _on_mem_threshold_changed(self, which: str, slider_int: int):
        """Called on slider move; persists settings and applies them to live memory."""
        if which == 'assoc':
            value = slider_int
            self._assoc_val_lbl.setText(str(value))
            if self._settings:
                self._settings.set_max_associations(value)
            logger.info('[AdvancedSettings] max_associations set to %d', value)
        else:
            value = slider_int / 100
            if which == 'stm':
                self._stm_val_lbl.setText(f'{value:.2f}')
                if self._settings:
                    self._settings.set_stm_threshold(value)
                logger.info('[AdvancedSettings] stm_new_center_threshold set to %.3f', value)
            else:
                self._ltm_val_lbl.setText(f'{value:.2f}')
                if self._settings:
                    self._settings.set_ltm_threshold(value)
                logger.info('[AdvancedSettings] ltm_new_center_threshold set to %.3f', value)
        self._apply_mem_thresholds()

    def _apply_mem_thresholds(self):
        """Pushes current threshold values to the running biomem memory engine."""
        if not self._settings:
            return
        stm_v = self._settings.get_stm_threshold()
        ltm_v = self._settings.get_ltm_threshold()
        assoc_v = self._settings.get_max_associations()
        self._schedule_async_cmd('set_mem_thresholds', {
            'stm_new_center_threshold': stm_v,
            'ltm_new_center_threshold': ltm_v,
            'max_associations': assoc_v,
        })

    def _on_reset_mem_defaults(self):
        """Resets memory settings sliders and variables to default values."""
        if not self._settings:
            return
        self._settings.set_max_associations(5)
        self._settings.set_stm_threshold(0.5)
        self._settings.set_ltm_threshold(0.78)
        self._stm_slider.blockSignals(True)
        self._ltm_slider.blockSignals(True)
        if hasattr(self, '_assoc_slider'):
            self._assoc_slider.blockSignals(True)
        if hasattr(self, '_assoc_slider'):
            self._assoc_slider.setValue(5)
            self._assoc_val_lbl.setText('5')
        self._stm_slider.setValue(round(50))
        self._ltm_slider.setValue(round(78))
        self._stm_val_lbl.setText('0.50')
        self._ltm_val_lbl.setText('0.78')
        self._stm_slider.blockSignals(False)
        self._ltm_slider.blockSignals(False)
        if hasattr(self, '_assoc_slider'):
            self._assoc_slider.blockSignals(False)
        logger.info('[AdvancedSettings] Memory defaults restored (assoc=5, stm=0.50, ltm=0.78)')
        self._apply_mem_thresholds()
        if hasattr(self, '_set_status'):
            self._set_status(T('memory.center_saved'), '', _COLORS['success'])

    def _on_backup(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            T('module.backup'),
            '',
            f"{T('tray.title')} (*.bdbm);;All Files (*.*)",
        )
        if path:
            self._schedule_async_cmd('backup', {'path': path})

    def _on_restore(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            T('module.restore'),
            '',
            f"{T('tray.title')} (*.bdbm);;Legacy PyTorch (*.pt);;All Files (*.*)",
        )
        if not path:
            return
        reply = QMessageBox.question(
            self,
            T('module.restore'),
            T('msg.restore_confirm'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._schedule_async_cmd('restore', {'path': path})

    def _on_import_legacy_pt(self):
        locked = bool(self._settings and getattr(self._settings, 'pt_import_locked', False))
        if locked or self._memory_stats.get('writes', 0) > 0:
            QMessageBox.warning(self, T('msg.import_blocked_title'), T('msg.import_blocked_msg'))
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            T('memory.import_pt'),
            '',
            'PyTorch State (*.pt);;All Files (*.*)',
        )
        if path:
            self._schedule_async_cmd('restore', {'path': path})

    def _on_clear_stm(self):
        reply = QMessageBox.question(
            self,
            T('memory.clear_stm'),
            T('msg.clear_stm_confirm'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._schedule_async_cmd('clear_stm', {})

    def _on_clear_all(self):
        reply = QMessageBox.question(
            self,
            T('ui.clear_all'),
            T('msg.clear_all_confirm'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._schedule_async_cmd('clear_ltm', {})

    def _on_refactor_memory(self):
        reply = QMessageBox.question(
            self,
            T('memory.refactor_title'),
            T('msg.refactor_confirm'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._refactor_dlg = RefactorProgressDialog(self)
            self._refactor_dlg.show()
            if hasattr(self, '_command_handler') and hasattr(self._command_handler, 'set_refactor_progress_callback'):

                def _cb(step, current=0, total=0, detail=''):
                    self.post_message(MSG_REFACTOR_PROGRESS, {
                        'step': step,
                        'current': current,
                        'total': total,
                        'detail': detail,
                    })

                self._command_handler.set_refactor_progress_callback(_cb)
            self._schedule_async_cmd('refactor_memory', {})

    def _on_export_product(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            T('memory.export_product'),
            '',
            f"{T('tray.title')} (*.bdbm);;All Files (*.*)",
        )
        if path:
            self._schedule_async_cmd('export_product', {'path': path})

    def _on_export_report(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            T('memory.export_report'),
            '',
            'PDF Report (*.pdf);;All Files (*.*)',
        )
        if path:
            self._schedule_async_cmd('generate_cognitive_report', {'path': path})

    def _on_show_dendrogram(self):
        """Opens the memory dendrogram window."""
        if not (hasattr(self, '_command_handler') and self._async_loop):
            QMessageBox.information(
                self,
                T('memory.dendrogram_title'),
                T('memory.dendrogram_error', 'Module not ready yet.'),
            )
            return
        try:
            if self._dendrogram_dlg is not None and self._dendrogram_dlg.isVisible():
                self._dendrogram_dlg.activateWindow()
                self._dendrogram_dlg.raise_()
                return
        except RuntimeError:
            self._dendrogram_dlg = None
        dlg = DendrogramWindow(self, self._command_handler, self._async_loop)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.finished.connect(lambda _: setattr(self, '_dendrogram_dlg', None))
        dlg.show()
        self._dendrogram_dlg = dlg

    def _on_show_temporal_evolution(self):
        """Opens the cognitive development time axis window."""
        if not (hasattr(self, '_command_handler') and self._async_loop):
            QMessageBox.information(
                self,
                T('memory.temporal_title'),
                T('memory.temporal_error', 'Module not ready yet.'),
            )
            return
        try:
            if self._temporal_dlg is not None and self._temporal_dlg.isVisible():
                self._temporal_dlg.activateWindow()
                self._temporal_dlg.raise_()
                return
        except RuntimeError:
            self._temporal_dlg = None
        dlg = TemporalEvolutionWindow(self, self._command_handler, self._async_loop)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.finished.connect(lambda _: setattr(self, '_temporal_dlg', None))
        dlg.show()
        self._temporal_dlg = dlg

    def _on_show_graph(self):
        """Opens the semantic map of centers window."""
        if not (hasattr(self, '_command_handler') and self._async_loop):
            QMessageBox.information(
                self,
                T('memory.graph_title'),
                T('memory.graph_error', 'Module not ready yet.'),
            )
            return
        try:
            if hasattr(self, '_graph_dlg') and self._graph_dlg is not None and self._graph_dlg.isVisible():
                self._graph_dlg.activateWindow()
                self._graph_dlg.raise_()
                return
        except RuntimeError:
            self._graph_dlg = None
        dlg = GraphMapWindow(self, self._command_handler, self._async_loop)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.finished.connect(lambda _: setattr(self, '_graph_dlg', None))
        dlg.show()
        self._graph_dlg = dlg

    def _on_lang_changed(self, lang: str):
        from .localization import Localization
        if Localization._lang == lang:
            return
        Localization.set_language(lang)
        if self._settings:
            self._settings.set_ui_language(lang)
        if hasattr(self, '_tray') and self._tray:
            self._tray.update_title()
        self._update_lang_buttons()
        self._retranslate_ui()

    def _update_lang_buttons(self):
        from .localization import Localization
        curr = Localization._lang
        for btn, code in (
            (self.btn_en, 'en'),
            (self.btn_cz, 'cz'),
            (self.btn_de, 'de'),
            (self.btn_fr, 'fr'),
            (self.btn_pl, 'pl'),
        ):
            btn.setProperty('active', 'true' if curr == code else 'false')
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _retranslate_ui(self):
        """Updates texts across the whole UI according to the current language."""
        self.title_lbl.setText(T('ui.title_sub'))
        self.tabs.setTabText(0, T('nav.chat'))
        self.tabs.setTabText(1, T('nav.module'))
        self.tabs.setTabText(2, T('nav.memory'))
        self.tabs.setTabText(3, T('nav.llm_settings'))
        self.tabs.setTabText(4, T('nav.news'))
        self._retranslate_tab_chat()
        self._retranslate_tab_module()
        self._retranslate_tab_memory()
        self._retranslate_tab_llm()
        self._retranslate_tab_news()

    def _retranslate_tab_chat(self):
        if hasattr(self, 'chat_input'):
            self.chat_input.setPlaceholderText(T('ui.type_message'))
        if hasattr(self, 'chat_model_lbl'):
            self.chat_model_lbl.setText(T('ui.model'))
        if hasattr(self, 'new_chat_btn'):
            self.new_chat_btn.setText(T('ui.new_chat'))
        if hasattr(self, 'hist_lbl'):
            self.hist_lbl.setText(T('ui.recent_chats'))
        if hasattr(self, 'chat_mem_lbl'):
            self.chat_mem_lbl.setText(T('ui.memory'))
        if hasattr(self, 'chat_options_lbl'):
            self.chat_options_lbl.setText(T('ui.options'))
        if hasattr(self, 'dr_lbl'):
            self.dr_lbl.setText(T('ui.deep_recall'))
            self.dr_lbl.setToolTip(T('ui.deep_recall_tip'))
        if hasattr(self, 'ws_lbl'):
            self.ws_lbl.setText(T('ui.web_search'))
            self.ws_lbl.setToolTip(T('ui.web_search_tip'))
        if hasattr(self, 'attach_btn'):
            self.attach_btn.setToolTip(T('ui.attach'))
        self._thinking_phrases = [
            T('ui.thinking'),
            T('ui.remembering'),
            T('ui.boondoggling'),
            T('ui.shifting'),
            T('ui.seriousing'),
        ]
        if hasattr(self, 'chat_status_lbl'):
            curr = self.chat_status_lbl.text()
            if curr in ('Starting…', 'Startuji…'):
                self.chat_status_lbl.setText(T('ui.starting'))
                return
            if curr in ('Verifying…',):
                self.chat_status_lbl.setText(T('module.verifying'))
                return
            if curr in ('Not configured',):
                self.chat_status_lbl.setText(T('msg.not_configured'))
                return

    def _retranslate_tab_module(self):
        if hasattr(self, 'status_title'):
            self.status_title.setText(T('module.status_title'))
        if hasattr(self, 'backup_btn'):
            self.backup_btn.setText(T('module.backup'))
        if hasattr(self, 'restore_btn'):
            self.restore_btn.setText(T('module.restore'))
        if hasattr(self, 'shutdown_hint'):
            self.shutdown_hint.setText(T('module.shutdown_hint'))
        if hasattr(self, 'shutdown_btn'):
            self.shutdown_btn.setText(T('module.shutdown'))
        if self._server_ready:
            self._set_status(T('ui.ready'), '', _COLORS['success'])
        if hasattr(self, '_memory_stats') and self._memory_stats:
            self._update_memory_stats(self._memory_stats)

    def _retranslate_tab_memory(self):
        if hasattr(self, 'mem_title'):
            self.mem_title.setText(T('memory.title'))
        if hasattr(self, 'mig_lbl'):
            self.mig_lbl.setText(T('memory.legacy_import_hint'))
        if hasattr(self, 'mig_btn'):
            self.mig_btn.setText(T('memory.import_pt'))
        if hasattr(self, 'clear_stm_btn'):
            self.clear_stm_btn.setText(T('memory.clear_stm'))
        if hasattr(self, 'clear_all_btn'):
            self.clear_all_btn.setText(T('memory.clear_all'))
        if hasattr(self, 'export_product_btn'):
            self.export_product_btn.setText(T('memory.export_product'))
        if hasattr(self, 'export_report_btn'):
            self.export_report_btn.setText(T('memory.export_report'))
        if hasattr(self, 'show_dendrogram_btn'):
            self.show_dendrogram_btn.setText(T('memory.show_dendrogram'))
        if hasattr(self, 'show_temporal_btn'):
            self.show_temporal_btn.setText(T('memory.show_temporal'))
        if hasattr(self, 'show_graph_btn'):
            self.show_graph_btn.setText(T('memory.show_graph'))
        if hasattr(self, 'adv_warning'):
            self.adv_warning.setText(T('memory.advanced_warning'))
        if hasattr(self, 'assoc_name_lbl'):
            self.assoc_name_lbl.setText(T('memory.max_associations'))
        if hasattr(self, 'stm_name_lbl'):
            self.stm_name_lbl.setText(T('memory.stm_threshold'))
        if hasattr(self, 'ltm_name_lbl'):
            self.ltm_name_lbl.setText(T('memory.ltm_threshold'))
        if hasattr(self, 'reset_defaults_btn'):
            self.reset_defaults_btn.setText(T('memory.reset_defaults'))
        if hasattr(self, 'refactor_btn'):
            self.refactor_btn.setText(T('memory.refactor'))
        if hasattr(self, 'effect_lbl'):
            self.effect_lbl.setText(T('memory.effect_desc'))
        if hasattr(self, 'paap_title'):
            self.paap_title.setText(T('memory.export_product'))
        if hasattr(self, 'paap_badge'):
            self.paap_badge.setText(T('memory.paap_badge'))
        if hasattr(self, 'paap_desc'):
            self.paap_desc.setText(T('memory.paap_desc'))
        if hasattr(self, 'viz_title'):
            self.viz_title.setText(T('memory.card_viz_title'))
        if hasattr(self, 'viz_desc'):
            self.viz_desc.setText(T('memory.card_viz_desc'))
        if hasattr(self, 'params_title'):
            self.params_title.setText(T('memory.card_params_title'))
        if hasattr(self, 'maint_title'):
            self.maint_title.setText(T('memory.card_maint_title'))
        if hasattr(self, 'maint_desc'):
            self.maint_desc.setText(T('memory.card_maint_desc'))
        if hasattr(self, 'refactor_sub'):
            self.refactor_sub.setText(T('memory.refactor_subtitle'))
        if hasattr(self, 'clear_label'):
            self.clear_label.setText(T('memory.clear_subtitle'))

    def _retranslate_tab_llm(self):
        if hasattr(self, 'llm_title'):
            self.llm_title.setText(T('llm.title'))
        if hasattr(self, 'llm_subtitle'):
            self.llm_subtitle.setText(T('llm.subtitle'))
        if hasattr(self, 'save_llm_btn'):
            self.save_llm_btn.setText(T('llm.save_settings'))
        for model_id, group in self._llm_group_widgets.items():
            if 'key_label' in group:
                group['key_label'].setText(
                    T('llm.ollama_url') if model_id == 'ollama' else T('llm.api_key')
                )
            if 'model_label' in group:
                group['model_label'].setText(T('llm.model_name'))
            if 'context_label' in group:
                group['context_label'].setText(T('llm.context_window'))
            if 'timeout_label' in group:
                group['timeout_label'].setText(T('llm.request_timeout'))
            if 'personal_label' in group:
                group['personal_label'].setText(T('llm.system_prompt'))
            if 'ctx_val_lbl' in group:
                v = self._llm_context_sliders[model_id].value()
                group['ctx_val_lbl'].setText(
                    T('llm.disabled') if v == 0 else f'{v} {T("llm.words")}'
                )
            if 'to_val_lbl' in group:
                if self._ollama_timeout_slider:
                    v = self._ollama_timeout_slider.value()
                    group['to_val_lbl'].setText(f'{v} {T("llm.min")}')
            if 'model_input' in group:
                group['model_input'].setPlaceholderText(
                    f"{T('ui.default')}: {group['default_model']}"
                )
            if 'name_lbl' in group:
                dn = group['display_name']
                if 'Ollama' in dn:
                    dn = f'Ollama ({T("llm.local")})'
                group['name_lbl'].setText(dn)
            if 'personal_input' in group:
                group['personal_input'].setPlaceholderText(T('llm.placeholder_personal'))
            if 'link_lbl' in group:
                url = group['link_url']
                group['link_lbl'].setText(
                    f'<a href="{url}" style="color: {_COLORS["accent"]}; font-size: 12px;">{T("llm.get_api_key")}</a>'
                )

    def _retranslate_tab_news(self):
        if hasattr(self, 'news_title'):
            self.news_title.setText(T('news.title'))

    def _retranslate_ui(self):
        """Re-calls T() for all static UI elements and updates their text."""
        if hasattr(self, 'title_lbl'):
            self.title_lbl.setText(T('ui.title_sub'))
        self.tabs.setTabText(0, T('nav.chat'))
        self.tabs.setTabText(1, T('nav.module'))
        self.tabs.setTabText(2, T('nav.memory'))
        self.tabs.setTabText(3, T('nav.llm_settings'))
        self.tabs.setTabText(4, T('nav.news'))
        self._retranslate_tab_chat()
        self._retranslate_tab_module()
        self._retranslate_tab_memory()
        self._retranslate_tab_llm()
        self._retranslate_tab_news()

    def _on_quit_module(self):
        reply = QMessageBox.question(
            self,
            T('msg.shutdown_title'),
            T('msg.shutdown_confirm'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        logger.info('Dashboard: user is quitting the module')
        if self._on_quit:
            self._on_quit()
        app = QApplication.instance()
        if app:
            app.quit()
            return
        self.close()

    def set_async_loop(self, loop):
        self._async_loop = loop
        if self._conv_handler:
            self._conv_handler.set_async_loop(loop)

    def set_server_task(self, task) -> None:
        """Records the long-running server task owned by the background loop."""
        self._server_task = task
        if task is not None and self._shutdown_requested:
            loop = self._async_loop
            if loop and loop.is_running():
                loop.call_soon_threadsafe(task.cancel)

    def request_server_shutdown(self) -> None:
        """Requests graceful server cancellation from the Qt main thread."""
        self._shutdown_requested = True
        loop = self._async_loop
        task = self._server_task
        if loop and task and not task.done() and loop.is_running():
            loop.call_soon_threadsafe(task.cancel)

    def set_tray_icon(self, tray):
        self._tray = tray

    def set_command_handler(self, handler):
        self._command_handler = handler

    def _schedule_async_cmd(self, command: str, params: dict):
        if not (self._async_loop and hasattr(self, '_command_handler')):
            logger.warning(f"Dashboard: cannot execute '{command}' — server not ready")
            return

        async def _run():
            try:
                msg = {'command': command, **params}
                result = await self._command_handler.handle(msg)
                if command == 'backup':
                    self.post_message(MSG_BACKUP_DONE, result)
                    return
                if command == 'restore':
                    self.post_message(MSG_RESTORE_DONE, result)
                    return
                if command == 'export_product':
                    self.post_message(MSG_EXPORT_DONE, result)
                    return
                if command == 'generate_cognitive_report':
                    self.post_message(MSG_REPORT_DONE, result)
                    return
                if command in ('clear_stm', 'clear_ltm'):
                    self.post_message(MSG_STATUS_UPDATE, {
                        'text': 'READY',
                        'detail': 'Memory cleared.',
                        'color': _COLORS['success'],
                    })
                    return
                if command == 'refactor_memory':
                    self.post_message(MSG_REFACTOR_DONE, result)
                    return
                if command == 'set_mem_thresholds':
                    return
            except Exception as e:
                logger.error(f"Dashboard async cmd '{command}': {e}")
                if command == 'refactor_memory':
                    self.post_message(MSG_REFACTOR_DONE, {'status': 'error', 'error': str(e)})

        loop = self._async_loop
        loop.call_soon_threadsafe(asyncio.ensure_future, _run())

    @staticmethod
    def _html_escape(text: str) -> str:
        return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

    @staticmethod
    def _markdown_to_html(text: str) -> str:
        """Convert markdown to HTML. Uses 'markdown' library if available, else simple regex."""
        try:
            import markdown as md_lib
            return md_lib.markdown(text, extensions=['fenced_code', 'tables'])
        except ImportError:
            import re
            html = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            html = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', html)
            html = re.sub(r'\*(.+?)\*', r'<i>\1</i>', html)
            html = re.sub(
                r'`(.+?)`',
                r'<code style="background:#f1f5f9;padding:2px 6px;border-radius:4px;">\1</code>',
                html,
            )
            html = re.sub(r'^### (.+)$', r'<h4>\1</h4>', html, flags=re.MULTILINE)
            html = re.sub(r'^## (.+)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
            html = re.sub(r'^# (.+)$', r'<h2>\1</h2>', html, flags=re.MULTILINE)
            html = re.sub(r'^- (.+)$', r'<li>\1</li>', html, flags=re.MULTILINE)
            html = html.replace('\n\n', '<br><br>').replace('\n', '<br>')
            return html


def show_notification(title: str, message: str, level: str = 'info'):
    def _show():
        app = QApplication.instance()
        _created = False
        if not app:
            app = QApplication(sys.argv)
            _created = True
        msg = QMessageBox()
        msg.setWindowTitle(title)
        msg.setText(message)
        if level == 'error':
            msg.setIcon(QMessageBox.Icon.Critical)
        elif level == 'warning':
            msg.setIcon(QMessageBox.Icon.Warning)
        else:
            msg.setIcon(QMessageBox.Icon.Information)
        msg.exec()
        if _created:
            app.quit()

    app = QApplication.instance()
    if app:
        if threading.current_thread() is threading.main_thread():
            _show()
            return
        QTimer.singleShot(0, _show)
        return
    threading.Thread(target=_show, daemon=True).start()


def fetch_news_async(settings_manager, dashboard: BDBMDashboard):
    import urllib.request
    import time
    from .net import build_ssl_context

    def _fetch():
        try:
            url = ''
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=8, context=build_ssl_context()) as resp:
                raw = resp.read().decode('utf-8')
            if '|' in raw:
                news_id, content = raw.split('|', 1)
                news_id = news_id.strip()
            else:
                news_id = ''
                content = raw
            stored_id = settings_manager.last_news_id
            if news_id != stored_id or not stored_id:
                dashboard.post_message(MSG_NEWS_LOADED, {'content': content, 'news_id': news_id})
        except Exception as e:
            logger.debug(f'News: failed to fetch ({e})')

    threading.Thread(target=_fetch, daemon=True, name='bdbm-news').start()
