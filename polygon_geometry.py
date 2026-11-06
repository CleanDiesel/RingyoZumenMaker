"""Reconstruct polygon shells/holes and measure exterior perimeters only."""
from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer
from .drawing_inputs import DrawingError
from .drawing_overrides import context_for, evaluate


def validate_polygon_vertices(point_layer, expression):
    context = context_for(point_layer)
    groups = {}
    for feature in point_layer.getFeatures():
        context.setFeature(feature)
        value = evaluate(expression, context, 'パートの区分') if expression.strip() else None
        coordinates = groups.setdefault(repr(value), set())
        coordinates.update((point.x(), point.y()) for point in feature.geometry().vertices())
    if not groups or any(len(points) < 3 for points in groups.values()):
        raise DrawingError('3点未満のパートがあります。抽出条件・地物の区分・パートの区分を確認してください。')


def polygon_from_paths(line_layer):
    rings = []
    for feature in line_layer.getFeatures():
        geometry = feature.geometry()
        paths = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
        for path in paths:
            if len(path) < 4 or path[0] != path[-1]:
                raise DrawingError('ポリゴンの各パートには、輪郭順に並んだ3点以上の測点が必要です。')
            polygon = QgsGeometry.fromPolygonXY([path])
            if not polygon.isGeosValid() or polygon.area() <= 0:
                raise DrawingError('ポリゴンの輪郭が交差しているか面積がありません。結合順とパートの区分を確認してください。')
            equal = getattr(polygon, 'isTopologicallyEqual', None) or polygon.isGeosEqual
            if any(equal(other['geometry']) for other in rings):
                continue
            rings.append({'points': path, 'geometry': polygon, 'area': polygon.area()})
    if not rings:
        raise DrawingError('有効なポリゴンの輪郭がありません。')
    # The nearest containing ring is the parent. Even depths are shells, odd
    # depths are holes; islands nested inside holes become shells again.
    parents = []
    for ring in rings:
        enclosing = [i for i, candidate in enumerate(rings)
                     if candidate['area'] > ring['area'] and candidate['geometry'].contains(ring['geometry'])]
        parents.append(min(enclosing, key=lambda i: rings[i]['area']) if enclosing else None)
    depths = []
    for parent in parents:
        depth = 0
        while parent is not None:
            depth += 1
            parent = parents[parent]
        depths.append(depth)
    polygons = []
    for index, ring in enumerate(rings):
        if depths[index] % 2:
            continue
        holes = [rings[i]['points'] for i, parent in enumerate(parents) if parent == index]
        polygons.append(QgsGeometry.fromPolygonXY([ring['points']] + holes))
    geometry = QgsGeometry.unaryUnion(polygons)
    if geometry.isNull() or geometry.isEmpty() or not geometry.isGeosValid():
        raise DrawingError('ポリゴンを構築できません。輪郭の重なりや交差を確認してください。')
    geometry.convertToMultiType()
    return geometry


def exterior_perimeter(geometry):
    parts = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
    return sum(QgsGeometry.fromPolylineXY(rings[0]).length() for rings in parts if rings)


def geometry_layer(source, geometry, geometry_type):
    layer = QgsVectorLayer(geometry_type, source.name(), 'memory')
    layer.setCrs(source.crs())
    provider = layer.dataProvider()
    provider.addAttributes(list(source.fields()))
    layer.updateFields()
    feature = QgsFeature(layer.fields())
    original = next(source.getFeatures(), None)
    if original is not None:
        feature.setAttributes(original.attributes())
    feature.setGeometry(geometry)
    if not provider.addFeature(feature):
        raise DrawingError('ポリゴンの計算結果を作成できません。')
    layer.updateExtents()
    return layer


def reconstruct_polygon_layers(line_layer, area_layer):
    geometry = polygon_from_paths(line_layer)
    paths = [ring for rings in geometry.asMultiPolygon() for ring in rings]
    line = geometry_layer(line_layer, QgsGeometry.fromMultiPolylineXY(paths), 'MultiLineString')
    area = geometry_layer(area_layer, geometry, 'MultiPolygon')
    return line, area, exterior_perimeter(geometry)
