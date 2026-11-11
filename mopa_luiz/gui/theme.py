"""One set of colour tokens, a stylesheet built from them, and painted icons."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap

DARK = {
    "bg": "#15171b", "panel": "#1d2025", "raised": "#262a31", "hover": "#2f343d", "border": "#353a45",
    "text": "#e7e9ed", "muted": "#99a1ae", "faint": "#6b7380",
    "accent": "#4c9aff", "accent_text": "#08111f", "accent_soft": "#1c3152",
    "danger": "#e5484d", "danger_text": "#ffffff", "success": "#46b85f", "warning": "#e0a030",
    "canvas": "#0f1114", "grid": "#1f2329", "grid_major": "#2a2f38", "field": "#566070", "outside": "#1a0f10",
    "copper": "#b87333", "copper_fill": "#5a3a1c", "removal": "#e0564a", "contour": "#ff9a52",
    "mask": "#3fb68b", "silk": "#d9dbe6", "toolpath": "#63b3ff", "reference": "#f2c94c",
    "target": "#ff4d6d", "artwork": "#7dd3c0",
}
LIGHT = {
    "bg": "#eef0f3", "panel": "#ffffff", "raised": "#f5f6f8", "hover": "#e9ecf1", "border": "#cfd4dc",
    "text": "#1a1e24", "muted": "#566070", "faint": "#8a93a0",
    "accent": "#1f6feb", "accent_text": "#ffffff", "accent_soft": "#dbe8ff",
    "danger": "#cf222e", "danger_text": "#ffffff", "success": "#1a7f37", "warning": "#9a6700",
    "canvas": "#fbfbfc", "grid": "#eceef2", "grid_major": "#dde1e7", "field": "#7a8494", "outside": "#f7e9e9",
    "copper": "#a35f22", "copper_fill": "#e9c9a6", "removal": "#c93a2e", "contour": "#d96a1c",
    "mask": "#1f8f68", "silk": "#4a4f63", "toolpath": "#1f6feb", "reference": "#a67700",
    "target": "#d6204a", "artwork": "#147a66",
}
THEMES = {"dark": DARK, "light": LIGHT}
_current = dict(DARK)
_name = "dark"


def tokens() -> dict[str, str]:
    return _current


def name() -> str:
    return _name


def color(token: str, alpha: int | None = None) -> QColor:
    result = QColor(_current[token])
    if alpha is not None:
        result.setAlpha(alpha)
    return result


def system_theme() -> str:
    try:
        return "light" if QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Light else "dark"
    except AttributeError:
        return "dark"


def stylesheet(t: dict[str, str], check: str = "") -> str:
    return f"""
* {{ font-size: 10pt; }}
QWidget {{ background: {t['bg']}; color: {t['text']}; }}
QMainWindow::separator {{ background: {t['border']}; width: 1px; height: 1px; }}
QToolTip {{ background: {t['raised']}; color: {t['text']}; border: 1px solid {t['border']}; padding: 5px 7px; }}
QMenuBar {{ background: {t['panel']}; border-bottom: 1px solid {t['border']}; }}
QMenuBar::item {{ padding: 6px 10px; background: transparent; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {t['hover']}; }}
QMenu {{ background: {t['panel']}; border: 1px solid {t['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 28px 6px 14px; border-radius: 4px; }}
QMenu::item:disabled {{ color: {t['faint']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 8px; }}
QToolBar {{ background: {t['panel']}; border: none; border-bottom: 1px solid {t['border']}; padding: 6px 8px; spacing: 6px; }}
QToolBar::separator {{ background: {t['border']}; width: 1px; margin: 4px 6px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 6px 10px; }}
QToolButton:hover {{ background: {t['hover']}; }}
QToolButton:pressed, QToolButton:checked {{ background: {t['accent_soft']}; border-color: {t['accent']}; }}
QToolButton:disabled {{ color: {t['faint']}; }}
QToolButton#start {{ background: {t['success']}; color: #ffffff; font-weight: 600; padding: 6px 16px; }}
QToolButton#start:disabled {{ background: {t['raised']}; color: {t['faint']}; }}
QToolButton#stop {{ background: {t['danger']}; color: {t['danger_text']}; font-weight: 700; padding: 5px 18px; border-radius: 6px; }}
QToolButton#stop:hover {{ background: #f0595e; }}
QToolButton#arm:checked {{ background: {t['warning']}; color: #1a1200; border-color: {t['warning']}; font-weight: 600; }}
QDockWidget {{ titlebar-close-icon: none; titlebar-normal-icon: none; }}
QDockWidget::title {{ background: {t['panel']}; padding: 8px 10px; border-bottom: 1px solid {t['border']};
    font-weight: 600; }}
QFrame#panel, QWidget#panel {{ background: {t['panel']}; }}
QWidget#section {{ background: transparent; }}
QWidget#corner {{ background: {t['panel']}; }}
QLabel {{ background: transparent; }}
QLabel#heading {{ font-size: 9pt; font-weight: 700; color: {t['muted']}; letter-spacing: 1px; padding-top: 6px; }}
QLabel#title {{ font-size: 13pt; font-weight: 600; }}
QLabel#muted, QLabel#hint {{ color: {t['muted']}; }}
#chip {{ border-radius: 10px; padding: 3px 10px; font-weight: 600; background: {t['raised']};
    border: 1px solid {t['border']}; }}
#chip::menu-indicator {{ image: none; width: 0; }}
#chip[state="ok"] {{ background: {t['success']}; color: #ffffff; border-color: {t['success']}; }}
#chip[state="bad"] {{ background: {t['danger']}; color: #ffffff; border-color: {t['danger']}; }}
#chip[state="warn"] {{ background: {t['warning']}; color: #1a1200; border-color: {t['warning']}; }}
QLabel#issue[level="error"] {{ color: {t['danger']}; }}
QLabel#issue[level="warning"] {{ color: {t['warning']}; }}
QFrame#banner {{ background: {t['danger']}; border-radius: 6px; }}
QFrame#banner QLabel {{ color: #ffffff; font-weight: 600; }}
QFrame#banner QPushButton {{ background: #ffffff; color: {t['danger']}; border: none; font-weight: 600; }}
QFrame#card {{ background: {t['raised']}; border: 1px solid {t['border']}; border-radius: 8px; }}
QPushButton {{ background: {t['raised']}; border: 1px solid {t['border']}; border-radius: 6px; padding: 6px 12px; }}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:disabled {{ color: {t['faint']}; }}
QPushButton#primary, QPushButton:default {{ background: {t['accent']}; color: {t['accent_text']};
    border-color: {t['accent']}; font-weight: 600; }}
QPushButton#primary:disabled {{ background: {t['raised']}; color: {t['faint']}; border-color: {t['border']}; }}
QPushButton#flat {{ background: transparent; border: none; color: {t['accent']}; padding: 4px 6px; }}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {t['bg']}; border: 1px solid {t['border']}; border-radius: 5px; padding: 4px 6px;
    selection-background-color: {t['accent']}; selection-color: {t['accent_text']}; }}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {t['accent']}; }}
QLineEdit:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {t['faint']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {t['panel']}; border: 1px solid {t['border']};
    selection-background-color: {t['hover']}; selection-color: {t['text']}; }}
QCheckBox, QRadioButton {{ background: transparent; spacing: 7px; }}
QCheckBox::indicator, QTreeView::indicator {{ width: 15px; height: 15px; border: 1px solid {t['faint']}; border-radius: 4px; background: {t['bg']}; }}
QCheckBox::indicator:hover, QTreeView::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked, QTreeView::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; image: url({check}); }}
QCheckBox::indicator:disabled {{ border-color: {t['border']}; background: {t['raised']}; }}
QDoubleSpinBox::up-button, QSpinBox::up-button, QDoubleSpinBox::down-button, QSpinBox::down-button {{ width: 0; border: none; }}
QTreeWidget, QListWidget, QTableWidget {{ background: {t['panel']}; border: none; outline: none;
    alternate-background-color: {t['raised']}; }}
QTreeWidget::item, QListWidget::item {{ padding: 5px 4px; border-radius: 4px; }}
QTreeWidget::item:selected, QListWidget::item:selected, QTableWidget::item:selected {{
    background: {t['accent_soft']}; color: {t['text']}; }}
QTreeWidget::item:hover, QListWidget::item:hover {{ background: {t['hover']}; }}
QHeaderView::section {{ background: {t['raised']}; border: none; border-bottom: 1px solid {t['border']};
    padding: 5px 8px; font-weight: 600; }}
QTableWidget {{ gridline-color: {t['border']}; }}
QProgressBar {{ background: {t['raised']}; border: none; border-radius: 4px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 4px; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle {{ background: {t['border']}; border-radius: 5px; min-height: 28px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {t['faint']}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; height: 0; width: 0; }}
QStatusBar {{ background: {t['panel']}; border-top: 1px solid {t['border']}; color: {t['muted']}; }}
QStatusBar::item {{ border: none; }}
QGroupBox {{ border: 1px solid {t['border']}; border-radius: 8px; margin-top: 12px; padding: 12px 10px 10px 10px;
    background: transparent; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; color: {t['muted']}; }}
QTabWidget::pane {{ border: 1px solid {t['border']}; border-radius: 6px; top: -1px; }}
QTabBar::tab {{ background: transparent; padding: 7px 14px; border-bottom: 2px solid transparent; color: {t['muted']}; }}
QTabBar::tab:selected {{ color: {t['text']}; border-bottom-color: {t['accent']}; }}
QSplitter::handle {{ background: {t['border']}; }}
QGraphicsView {{ border: none; }}
"""


def apply(app, theme: str | None = None) -> str:
    """Style the whole application. Called from main() and from tests, so both look the same."""
    global _current, _name
    _name = theme if theme in THEMES else system_theme()
    _current = dict(THEMES[_name])
    app.setStyle("Fusion")
    font = QFont("Segoe UI")
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setPointSize(10)
    app.setFont(font)
    palette = QPalette()
    roles = QPalette.ColorRole
    for role, token in ((roles.Window, 'bg'), (roles.Base, 'bg'), (roles.AlternateBase, 'raised'),
                        (roles.Button, 'raised'), (roles.Text, 'text'), (roles.WindowText, 'text'),
                        (roles.ButtonText, 'text'), (roles.Highlight, 'accent'),
                        (roles.HighlightedText, 'accent_text'), (roles.ToolTipBase, 'raised'),
                        (roles.ToolTipText, 'text'), (roles.PlaceholderText, 'faint'), (roles.Link, 'accent')):
        palette.setColor(role, color(token))
    for role in (roles.Text, roles.WindowText, roles.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, color('faint'))
    app.setPalette(palette)
    app.setStyleSheet(stylesheet(_current, _check_image()))
    return _name


def _check_image() -> str:
    """A white tick written to a temp file, because a stylesheet indicator needs an image URL."""
    import tempfile
    from pathlib import Path

    path = Path(tempfile.gettempdir()) / 'studiolaser-check.png'
    pixmap = QPixmap(30, 30)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor('#ffffff'), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin))
    tick = QPainterPath(QPointF(7, 16))
    tick.lineTo(QPointF(13, 22))
    tick.lineTo(QPointF(23, 9))
    painter.drawPath(tick)
    painter.end()
    try:
        pixmap.save(str(path))
    except OSError:
        return ''
    return path.as_posix()


def icon(kind: str, token: str = "text", size: int = 20) -> QIcon:
    """Small line icons painted from primitives, so they follow the theme and need no asset files."""
    ratio = 2
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    ink = QColor(token) if token.startswith("#") else color(token)
    pen = QPen(ink, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    s = size

    def poly(*points, close=False, fill=False):
        path = QPainterPath(QPointF(*points[0]))
        for point in points[1:]:
            path.lineTo(QPointF(*point))
        if close:
            path.closeSubpath()
        if fill:
            painter.fillPath(path, ink)
        painter.drawPath(path)

    if kind == "play":
        poly((s * .3, s * .2), (s * .8, s * .5), (s * .3, s * .8), close=True, fill=True)
    elif kind == "stop":
        painter.fillRect(QRectF(s * .25, s * .25, s * .5, s * .5), ink)
    elif kind == "frame":
        for (x, y, dx, dy) in ((.2, .2, 1, 1), (.8, .2, -1, 1), (.2, .8, 1, -1), (.8, .8, -1, -1)):
            poly((s * (x + .2 * dx), s * y), (s * x, s * y), (s * x, s * (y + .2 * dy)))
        painter.drawEllipse(QPointF(s * .5, s * .5), s * .07, s * .07)
    elif kind == "arm":
        painter.drawEllipse(QPointF(s * .5, s * .5), s * .32, s * .32)
        poly((s * .36, s * .5), (s * .46, s * .6), (s * .65, s * .4))
    elif kind == "fit":
        painter.drawRect(QRectF(s * .28, s * .32, s * .44, s * .36))
        for (x, y, dx, dy) in ((.12, .16, 1, 1), (.88, .16, -1, 1), (.12, .84, 1, -1), (.88, .84, -1, -1)):
            poly((s * (x + .12 * dx), s * y), (s * x, s * y), (s * x, s * (y + .12 * dy)))
    elif kind == "board":
        painter.drawRoundedRect(QRectF(s * .15, s * .22, s * .7, s * .56), 2, 2)
        poly((s * .28, s * .62), (s * .28, s * .42), (s * .5, s * .42), (s * .5, s * .58), (s * .72, s * .58))
        painter.drawEllipse(QPointF(s * .72, s * .38), s * .05, s * .05)
    elif kind == "import":
        poly((s * .5, s * .15), (s * .5, s * .6))
        poly((s * .32, s * .44), (s * .5, s * .62), (s * .68, s * .44))
        poly((s * .18, s * .66), (s * .18, s * .84), (s * .82, s * .84), (s * .82, s * .66))
    elif kind == "save":
        poly((s * .18, s * .18), (s * .68, s * .18), (s * .82, s * .32), (s * .82, s * .82), (s * .18, s * .82), close=True)
        painter.drawRect(QRectF(s * .32, s * .52, s * .36, s * .3))
    elif kind == "open":
        poly((s * .14, s * .78), (s * .14, s * .24), (s * .4, s * .24), (s * .48, s * .34), (s * .82, s * .34),
             (s * .82, s * .78), close=True)
    elif kind == "gear":
        painter.drawEllipse(QPointF(s * .5, s * .5), s * .14, s * .14)
        painter.drawEllipse(QPointF(s * .5, s * .5), s * .3, s * .3)
        for i in range(8):
            painter.save()
            painter.translate(s * .5, s * .5)
            painter.rotate(i * 45)
            painter.drawLine(QPointF(0, -s * .3), QPointF(0, -s * .4))
            painter.restore()
    elif kind == "target":
        painter.drawEllipse(QPointF(s * .5, s * .5), s * .26, s * .26)
        for (x0, y0, x1, y1) in ((.5, .1, .5, .34), (.5, .66, .5, .9), (.1, .5, .34, .5), (.66, .5, .9, .5)):
            painter.drawLine(QPointF(s * x0, s * y0), QPointF(s * x1, s * y1))
    elif kind == "preview":
        for y in (.3, .5, .7):
            poly((s * .2, s * y), (s * .8, s * y))
        painter.drawEllipse(QPointF(s * .8, s * .7), s * .06, s * .06)
    elif kind == "undo" or kind == "redo":
        flip = -1 if kind == "redo" else 1
        cx = s * .5
        path = QPainterPath(QPointF(cx - flip * s * .28, s * .42))
        path.cubicTo(QPointF(cx - flip * s * .05, s * .2), QPointF(cx + flip * s * .32, s * .34),
                     QPointF(cx + flip * s * .26, s * .72))
        painter.drawPath(path)
        poly((cx - flip * s * .3, s * .2), (cx - flip * s * .3, s * .44), (cx - flip * s * .08, s * .44))
    painter.end()
    return QIcon(pixmap)
