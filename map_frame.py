"""Reject clipped maps and record the rendered footprint for sheet layout."""
from qgis.PyQt.QtCore import QPointF, Qt
from qgis.PyQt.QtGui import QColor, QImage, QPainter, QBitmap, QRegion
from qgis.core import QgsCoordinateTransform
from .drawing_inputs import DrawingError

MAP_PADDING_MM = 3.0


def validate_map_frame(map_item, layers, title='地図'):
    points = []
    for layer in layers:
        transform = (QgsCoordinateTransform(layer.crs(), map_item.crs(), map_item.layout().project())
                     if layer.crs() != map_item.crs() else None)
        for feature in layer.getFeatures():
            for vertex in feature.geometry().vertices():
                point = transform.transform(vertex.x(), vertex.y()) if transform else vertex
                points.append(map_item.mapToItemCoords(QPointF(point.x(), point.y())))
    if not points:
        raise DrawingError(title + '：表示する測点・図形がありません。')
    width, height = map_item.rect().width(), map_item.rect().height()
    pad = MAP_PADDING_MM
    if (min(point.x() for point in points) < pad - 1e-6
            or min(point.y() for point in points) < pad - 1e-6
            or max(point.x() for point in points) > width - pad + 1e-6
            or max(point.y() for point in points) > height - pad + 1e-6):
        raise DrawingError(title + 'が地図枠内（内側余白3mm）に収まりません。'
                           '縮尺の分母を大きくするか、QGISレイアウトの表示範囲を調整してください。')


def rendered_map_bounds(path, width_mm, height_mm, title='地図'):
    image = QImage(str(path))
    if image.isNull():
        raise DrawingError('地図画像を検証できません：' + str(path))
    white = QImage(image.size(), QImage.Format.Format_RGB32)
    white.fill(QColor('white'))
    painter = QPainter(white)
    painter.drawImage(0, 0, image)
    painter.end()
    mask = white.createMaskFromColor(QColor('white').rgb(), Qt.MaskMode.MaskInColor)
    bounds = QRegion(QBitmap.fromImage(mask)).boundingRect()
    if bounds.isEmpty():
        raise DrawingError(title + '：地図画像に図形が描画されていません。')
    left = bounds.left() * width_mm / image.width()
    top = bounds.top() * height_mm / image.height()
    right = (bounds.right() + 1) * width_mm / image.width()
    bottom = (bounds.bottom() + 1) * height_mm / image.height()
    if (left < MAP_PADDING_MM or top < MAP_PADDING_MM
            or right > width_mm - MAP_PADDING_MM or bottom > height_mm - MAP_PADDING_MM):
        raise DrawingError(title + 'の図形またはラベルが地図枠内（内側余白3mm）に収まりません。'
                           '縮尺の分母を大きくするか、ラベルの位置を調整してください。')
    return [left * 150 / width_mm, top * 150 / height_mm,
            right * 150 / width_mm, bottom * 150 / height_mm]
