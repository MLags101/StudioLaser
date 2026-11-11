"""The work area: field, grid, rulers, the current job and its toolpath preview.

The scene is in millimetres with Y drawn upward, so every point is stored as
(x, -y). The job is one parent item whose transform is the placement, so a
move or rotation only updates that transform.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView

from ..layers import RASTER_ENGRAVE
from ..workspace import CLEANUP, COPPER_CONTOUR, COPPER_FILL, MASK, SILK
from . import theme

KIND_TOKEN = {COPPER_CONTOUR: "contour", COPPER_FILL: "removal", CLEANUP: "removal", MASK: "mask", SILK: "silk",
              "": "artwork"}
RULER = 22            # px
DRAG_THRESHOLD = 5    # px of travel before a press becomes a move
PICK_RADIUS = 14      # px
MAX_PREVIEW_SEGMENTS = 250_000


def painter_path(paths, *, closed=False) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    for points in paths:
        if len(points) < 2:
            continue
        path.moveTo(points[0][0], -points[0][1])
        for x, y in points[1:]:
            path.lineTo(x, -y)
        if closed:
            path.closeSubpath()
    return path


def cosmetic(token, width=1.2, style=Qt.PenStyle.SolidLine, alpha=None) -> QPen:
    pen = QPen(theme.color(token, alpha), width, style)
    pen.setCosmetic(True)
    return pen


def placement_transform(placement) -> QTransform:
    transform = QTransform()
    transform.translate(placement.x, -placement.y)
    transform.rotate(-placement.angle)
    transform.scale(-placement.scale if placement.mirror else placement.scale, placement.scale)
    return transform


class Canvas(QGraphicsView):
    cursor_moved = Signal(float, float)
    reference_picked = Signal(int)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(320, 240)
        self.picking = False          # registration: clicks choose a drilled hole
        self.picked: list[int] = []   # reference indices highlighted in order
        self._job_item = None
        self._preview_item = None
        self._target_item = None
        self._frame_item = None       # what the red light is tracing right now
        self._frame_key = None
        self._press = None            # (view pos, scene pos, placement) while the left button is down
        self._dragging = False
        self._pan = None
        self._hover = -1
        self._fitted = False
        controller.selection_changed.connect(self.rebuild_and_fit)
        controller.job_changed.connect(self.rebuild)
        controller.placement_changed.connect(self.update_placement)
        controller.plan_changed.connect(self.update_preview)
        controller.state_changed.connect(self.update_frame_overlay)
        controller.profile_changed.connect(self.rebuild_and_fit)
        self.rebuild()

    # ---- scene construction

    def field(self) -> QRectF:
        profile = self.controller.profile
        return QRectF(-profile.width / 2, -profile.height / 2, profile.width, profile.height)

    def rebuild_and_fit(self):
        self.rebuild()
        self.fit()

    def rebuild(self):
        scene = self.scene()
        scene.clear()
        self._job_item = self._preview_item = self._target_item = self._frame_item = None
        self._frame_key = None
        field = self.field()
        reach = max(field.width(), field.height()) * 3
        scene.setSceneRect(QRectF(-reach, -reach, 2 * reach, 2 * reach))
        self.setBackgroundBrush(theme.color("canvas"))
        job = self.controller.job
        if job is not None:
            self._job_item = self._build_job(job)
            scene.addItem(self._job_item)
            self._job_item.setTransform(placement_transform(job.placement))
        self.update_preview()
        self._build_target()
        self.update_frame_overlay()
        self.viewport().update()

    def _build_job(self, job) -> QGraphicsItem:
        root = QGraphicsRectItem()
        root.setPen(Qt.PenStyle.NoPen)

        def add(path, pen=Qt.PenStyle.NoPen, brush=Qt.BrushStyle.NoBrush, z=0.0):
            item = QGraphicsPathItem(path, root)
            item.setPen(pen)
            item.setBrush(brush)
            item.setZValue(z)
            return item

        if job.boundary:
            # On a board everything inside the outline is copper until the laser removes it.
            fill = theme.color("copper_fill", 150 if job.side else 30)
            add(painter_path(job.boundary, closed=True), cosmetic("field", 1.4), QBrush(fill), 0)
        if job.retained:
            add(painter_path(job.retained, closed=True), cosmetic("copper", 0.8), QBrush(theme.color("copper", 190)), 1)
        seen_fill = set()
        for artwork in job.objects:
            layer = artwork.layer
            if not layer.output or not layer.visible:
                continue
            token = KIND_TOKEN.get(artwork.kind, "artwork")
            if layer.operation == RASTER_ENGRAVE and artwork.fills:
                key = (token, id(artwork.fills))
                if artwork.kind == CLEANUP and any(k[0] == "removal" for k in seen_fill):
                    continue    # same area as the copper fill; drawing it twice only darkens it
                seen_fill.add(key)
                add(painter_path(artwork.fills, closed=True), cosmetic(token, 0.8),
                    QBrush(theme.color(token, 150)), 2)
            elif artwork.paths:
                add(painter_path(artwork.paths), cosmetic(token, 1.3), z=3)
        if job.references:
            holes = QPainterPath()
            for x, y in job.references:
                holes.addEllipse(QPointF(x, -y), 0.45, 0.45)
            add(holes, cosmetic("reference", 1.2), z=4)
        return root

    def update_placement(self):
        job = self.controller.job
        if self._job_item is not None and job is not None:
            self._job_item.setTransform(placement_transform(job.placement))
        self.update_frame_overlay()
        self.viewport().update()

    def update_preview(self):
        if self._preview_item is not None:
            self.scene().removeItem(self._preview_item)
            self._preview_item = None
        plan = self.controller.plan
        if plan is None:
            return
        paths = plan.preview
        segments = sum(len(path) - 1 for path in paths)
        if segments > MAX_PREVIEW_SEGMENTS:   # Draw an even sample; the plan itself is complete.
            paths = paths[::math.ceil(segments / MAX_PREVIEW_SEGMENTS)]
        self._preview_item = self.scene().addPath(painter_path(paths), cosmetic("toolpath", 1.0, alpha=200))
        self._preview_item.setZValue(10)

    def update_frame_overlay(self):
        """Draw the red-light path while framing, so the screen shows what the board shows."""
        c = self.controller
        framing = c.framing and not c.target
        job = c.job
        key = (framing, c.frame_mode, job.job_id if job else None, job.placement if job else None) if framing else None
        if key == self._frame_key:
            return
        self._frame_key = key
        if self._frame_item is not None:
            self.scene().removeItem(self._frame_item)
            self._frame_item = None
        if framing:
            pen = cosmetic("target", 1.6, Qt.PenStyle.DashLine)
            self._frame_item = self.scene().addPath(painter_path(c.frame_paths()), pen)
            self._frame_item.setZValue(19)
        self.viewport().update()

    def _build_target(self):
        target = self.controller.target
        if target:
            self._target_item = self.scene().addPath(painter_path(target), cosmetic("target", 2.0))
            self._target_item.setZValue(20)

    # ---- view

    def fit(self):
        job = self.controller.job
        rect = self.field()
        box = job.bounds() if job else None
        if box:
            x0, y0, x1, y1 = box
            target = QRectF(x0, -y1, x1 - x0, y1 - y0)
            pad = max(target.width(), target.height()) * 0.08 + 1
            target.adjust(-pad, -pad, pad, pad)
            # Show the job large, but never lose sight of where it sits in the field when it is nearly as big.
            small = target.width() < rect.width() * 0.6 and rect.contains(target)
            rect = target if small else rect.united(target).adjusted(-6, -6, 6, 6)
        else:
            rect = rect.adjusted(-6, -6, 6, 6)
        self.resetTransform()
        available = self.viewport().rect().adjusted(RULER, RULER, 0, 0)
        if available.width() > 10 and available.height() > 10:
            scale = min(available.width() / rect.width(), available.height() / rect.height())
            self.scale(scale, scale)
        self.centerOn(rect.center())
        self._fitted = True

    def fit_field(self):
        rect = self.field().adjusted(-8, -8, 8, 8)
        self.resetTransform()
        available = self.viewport().rect().adjusted(RULER, RULER, 0, 0)
        scale = min(available.width() / rect.width(), available.height() / rect.height())
        self.scale(scale, scale)
        self.centerOn(rect.center())

    def zoom(self, factor):
        current = self.transform().m11()
        factor = max(0.2 / current, min(600 / current, factor))
        self.scale(factor, factor)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._fitted or not self._user_zoomed():
            self.fit()

    def _user_zoomed(self) -> bool:
        return getattr(self, "_zoomed", False)

    def wheelEvent(self, event):
        self._zoomed = True
        self.zoom(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)

    # ---- painting

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, theme.color("canvas"))
        field = self.field()
        outside = QPainterPath()
        outside.addRect(rect)
        inner = QPainterPath()
        inner.addRect(field)
        painter.fillPath(outside.subtracted(inner), theme.color("outside"))
        scale = self.transform().m11()
        step = self._grid_step(scale)
        for spacing, token in ((step, "grid"), (step * 5, "grid_major")):
            pen = QPen(theme.color(token), 0)
            painter.setPen(pen)
            x = math.floor(max(rect.left(), field.left()) / spacing) * spacing
            while x <= min(rect.right(), field.right()):
                if x >= field.left():
                    painter.drawLine(QPointF(x, field.top()), QPointF(x, field.bottom()))
                x += spacing
            y = math.floor(max(rect.top(), field.top()) / spacing) * spacing
            while y <= min(rect.bottom(), field.bottom()):
                if y >= field.top():
                    painter.drawLine(QPointF(field.left(), y), QPointF(field.right(), y))
                y += spacing
        axis = QPen(theme.color("field", 150), 0, Qt.PenStyle.DashLine)
        painter.setPen(axis)
        painter.drawLine(QPointF(0, field.top()), QPointF(0, field.bottom()))
        painter.drawLine(QPointF(field.left(), 0), QPointF(field.right(), 0))
        painter.setPen(cosmetic("field", 1.6))
        painter.drawRect(field)

    @staticmethod
    def _grid_step(scale) -> float:
        """Minor grid spacing in mm so lines stay roughly 12 px or more apart."""
        for step in (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50):
            if step * scale >= 12:
                return step
        return 100

    def drawForeground(self, painter, rect):
        job = self.controller.job
        scale = self.transform().m11()
        painter.save()
        painter.resetTransform()
        view = self.viewport().rect()
        font = QFont(painter.font())
        font.setPointSizeF(8)
        painter.setFont(font)
        # Registration holes: numbers for picked ones, a ring under the cursor.
        if job is not None and job.references and (self.picking or self.picked):
            for index, point in enumerate(job.references):
                if index not in self.picked and index != self._hover:
                    continue
                pos = self.mapFromScene(self._scene_point(job.placement.apply(point)))
                painter.setPen(QPen(theme.color("target"), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(pos, 9, 9)
                if index in self.picked:
                    painter.setPen(theme.color("text"))
                    painter.drawText(pos.x() + 12, pos.y() - 6, str(self.picked.index(index) + 1))
        # Rulers.
        painter.fillRect(0, 0, view.width(), RULER, theme.color("panel"))
        painter.fillRect(0, 0, RULER, view.height(), theme.color("panel"))
        painter.setPen(QPen(theme.color("border"), 1))
        painter.drawLine(0, RULER, view.width(), RULER)
        painter.drawLine(RULER, 0, RULER, view.height())
        step = self._grid_step(scale) * 5
        top_left = self.mapToScene(0, 0)
        bottom_right = self.mapToScene(view.width(), view.height())
        painter.setPen(theme.color("muted"))
        x = math.ceil(top_left.x() / step) * step
        while x <= bottom_right.x():
            px = self.mapFromScene(QPointF(x, 0)).x()
            if px > RULER:
                painter.drawLine(px, RULER - 5, px, RULER)
                painter.drawText(px + 3, RULER - 8, f"{x:g}")
            x += step
        y = math.ceil(top_left.y() / step) * step
        while y <= bottom_right.y():
            py = self.mapFromScene(QPointF(0, y)).y()
            if py > RULER:
                painter.drawLine(RULER - 5, py, RULER, py)
                painter.save()
                painter.translate(RULER - 8, py - 3)
                painter.rotate(-90)
                painter.drawText(0, 0, f"{-y:g}")
                painter.restore()
            y += step
        painter.fillRect(0, 0, RULER, RULER, theme.color("panel"))
        painter.setPen(theme.color("faint"))
        painter.drawText(4, RULER - 8, "mm")
        if job is None:
            painter.setPen(theme.color("muted"))
            big = QFont(font)
            big.setPointSizeF(11)
            painter.setFont(big)
            painter.drawText(view.adjusted(RULER, RULER, 0, 0), Qt.AlignmentFlag.AlignCenter,
                             "Drop Gerber, drill, SVG or DXF files here\nor use File → Import")
        painter.restore()

    @staticmethod
    def _scene_point(point) -> QPointF:
        return QPointF(point[0], -point[1])

    # ---- interaction

    def _world(self, view_pos) -> tuple[float, float]:
        scene = self.mapToScene(view_pos)
        return scene.x(), -scene.y()

    def _nearest_reference(self, view_pos) -> int:
        job = self.controller.job
        best, best_distance = -1, PICK_RADIUS
        if job is None:
            return best
        for index, point in enumerate(job.references):
            pos = self.mapFromScene(self._scene_point(job.placement.apply(point)))
            distance = math.hypot(pos.x() - view_pos.x(), pos.y() - view_pos.y())
            if distance < best_distance:
                best, best_distance = index, distance
        return best

    def _over_job(self, view_pos) -> bool:
        job = self.controller.job
        box = job.bounds() if job else None
        if not box:
            return False
        x, y = self._world(view_pos)
        margin = 6 / self.transform().m11()
        return box[0] - margin <= x <= box[2] + margin and box[1] - margin <= y <= box[3] + margin

    def mousePressEvent(self, event):
        self.setFocus()
        pos = event.position().toPoint()
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self._pan = pos
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.LeftButton:
            if self.picking:
                index = self._nearest_reference(pos)
                if index >= 0:
                    self.reference_picked.emit(index)
            elif self.controller.job and self._over_job(pos) and not self.controller.blockers()["edit"]:
                self._press = (pos, self._world(pos), self.controller.job.placement)
                self._dragging = False
            else:
                self._pan = pos
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        x, y = self._world(pos)
        self.cursor_moved.emit(x, y)
        if self._pan is not None:
            delta = pos - self._pan
            self._pan = pos
            self._zoomed = True
            self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
            self.translate(delta.x() / self.transform().m11(), delta.y() / self.transform().m22())
            self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        elif self._press is not None:
            start, origin, placement = self._press
            if not self._dragging and (pos - start).manhattanLength() < DRAG_THRESHOLD:
                return
            self._dragging = True
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            from dataclasses import replace
            self.controller.set_placement(
                replace(placement, x=round(placement.x + x - origin[0], 3), y=round(placement.y + y - origin[1], 3)),
                merge="drag")
        elif self.picking:
            hover = self._nearest_reference(pos)
            if hover != self._hover:
                self._hover = hover
                self.viewport().update()
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover >= 0 else Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._over_job(pos) and not self.controller.marking
                           else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        self._pan = None
        self._press = None
        self._dragging = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        event.accept()

    def keyPressEvent(self, event):
        key, mods = event.key(), event.modifiers()
        arrows = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, 1), Qt.Key.Key_Down: (0, -1)}
        if key in arrows and self.controller.job and not self.picking:
            if self.controller.blockers()["edit"]:
                return
            fine = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            coarse = bool(mods & Qt.KeyboardModifier.AltModifier)
            dx, dy = arrows[key]
            if mods & Qt.KeyboardModifier.ControlModifier:
                step = 0.01 if fine else 1.0 if coarse else 0.1
                self.controller.nudge(angle=step * (dx or dy))
            else:
                step = 0.01 if fine else 1.0 if coarse else 0.1
                self.controller.nudge(dx * step, dy * step)
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._zoomed = True
            self.zoom(1.25)
        elif key == Qt.Key.Key_Minus:
            self._zoomed = True
            self.zoom(0.8)
        elif key in (Qt.Key.Key_F, Qt.Key.Key_Home):
            self._zoomed = False
            self.fit()
        else:
            super().keyPressEvent(event)
