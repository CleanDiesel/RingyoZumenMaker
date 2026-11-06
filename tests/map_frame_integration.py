"""Validate padding, rotations, preview, and malformed polygon parts."""
from pathlib import Path
import os, tempfile

ROOT = Path(__file__).resolve().parents[1]
exec((ROOT/'tests/measurement_integration.py').read_text(encoding='utf-8').split('from RingyoZumenMaker.main import Main')[0])
from RingyoZumenMaker.processing_provider import DrawingProvider, ALGORITHM_ID
from RingyoZumenMaker.drawing_service import DrawingService
from RingyoZumenMaker.drawing_inputs import DrawingError
from RingyoZumenMaker.map_frame import validate_map_frame

project=QgsProject.instance()
provider=DrawingProvider();QgsApplication.processingRegistry().addProvider(provider)
algorithm=QgsApplication.processingRegistry().algorithmById(ALGORITHM_ID)
context=QgsProcessingContext();context.setProject(project)
root=Path(tempfile.mkdtemp(prefix='map-frame-',dir=QA_ROOT))

def source(rings):
    layer=QgsVectorLayer('Point?crs=EPSG:6678','points','memory')
    layer.dataProvider().addAttributes([QgsField('part',QVariant.Int),QgsField('order',QVariant.Int)])
    layer.updateFields()
    for part,ring in enumerate(rings):
        for order,(x,y) in enumerate(ring):
            f=QgsFeature(layer.fields());f.setAttributes([part,order+23])
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x,y)));layer.dataProvider().addFeature(f)
    layer.updateExtents();project.addMapLayer(layer);return layer

def service(layer,paper=0):
    params={'LAND':layer,'LAND_PART':'"part"','LAND_ORDER':'"order"','LAND_LABEL':'"order"',
            'LAND_NAME':'','CRS':'EPSG:6678','SCALE':1000,'PAPER':paper,'CREATE_PROJECT':True,'OUTPUT':str(root)}
    return DrawingService(algorithm.request(params,context))

normal=source([[(0,0),(100,0),(100,100),(0,100)]])
owner=service(normal);owner.run(test=True)
assert not list(root.iterdir()), 'Preview must not create files'
layout=owner.create_layout([owner.role_pages['land'][0].line_layer])
item=next(item for item in layout.items() if isinstance(item,QgsLayoutItemMap))
item.setMapRotation(45)
validate_map_frame(item,[normal])
item.setScale(900)
try:
    validate_map_frame(item,[normal])
    raise AssertionError('A rotated 100m square at 1:900 must overflow')
except DrawingError as error:
    assert '3mm' in str(error)

near_edge=source([[(0,0),(145,0),(145,100),(0,100)]])
try:
    service(near_edge).run(test=True)
    raise AssertionError('A shape with less than 3mm padding must fail in preview')
except DrawingError as error:
    assert '3mm' in str(error)

large=source([[(0,0),(200,0),(200,100),(0,100)]])
service(large,paper=1).run(test=True)
for rings, expected in [([[(0,0),(10,0),(10,10),(0,10)],[(5,5)]],'3点未満'),
                        ([[(0,0),(10,10),(0,10),(10,0)]],'交差')]:
    try:
        service(source(rings)).run(test=True)
        raise AssertionError('Malformed polygon input was silently accepted')
    except DrawingError as error:
        assert expected in str(error), str(error)
assert not list(root.iterdir())
print('A4/A3, 3MM PADDING, ROTATION, PREVIEW AND INVALID PART CHECKS OK',flush=True)
print('PASS',flush=True)
os._exit(0)
