"""Independent checks: real Processing output -> PDF -> pixels -> m² and m."""
from pathlib import Path
import os, tempfile, subprocess, json, math

ROOT = Path(__file__).resolve().parents[1]
exec((ROOT / 'tests/measurement_integration.py').read_text(encoding='utf-8').split('from RingyoZumenMaker.main import Main')[0])
from qgis.PyQt.QtGui import QImage, QColor
from RingyoZumenMaker.processing_provider import DrawingProvider, ALGORITHM_ID
from RingyoZumenMaker.drawing_service import DrawingService
from RingyoZumenMaker.drawing_inputs import DrawingError
from RingyoZumenMaker.map_frame import rendered_map_bounds
import numpy as np

def square(x, y, size):
    return [(x,y),(x+size,y),(x+size,y+size),(x,y+size)]

def simplify_open(points, tolerance):
    points = np.asarray(points, dtype=float)
    keep = {0, len(points)-1}
    stack = [(0, len(points)-1)]
    while stack:
        start, end = stack.pop()
        if end <= start+1:
            continue
        delta = points[end] - points[start]
        norm = np.hypot(*delta)
        segment = points[start+1:end] - points[start]
        distances = (np.abs(segment[:,0]*delta[1] - segment[:,1]*delta[0]) / norm
                     if norm else np.hypot(segment[:,0], segment[:,1]))
        middle = int(np.argmax(distances)) + start+1
        if distances[middle-start-1] > tolerance:
            keep.add(middle)
            stack.extend([(start,middle),(middle,end)])
    return points[sorted(keep)]

def raster_exterior_perimeter(mask):
    # Trace directed exposed pixel edges, keeping the filled polygon on the
    # right. Outer loops have positive signed area; hole loops are negative.
    padded = np.pad(mask, 1)
    center = padded[1:-1,1:-1]
    sides = [(center & ~padded[:-2,1:-1], (0,0), (1,0), 0),
             (center & ~padded[1:-1,2:], (1,0), (1,1), 1),
             (center & ~padded[2:,1:-1], (1,1), (0,1), 2),
             (center & ~padded[1:-1,:-2], (0,1), (0,0), 3)]
    edges = {}
    for side, start, end, direction in sides:
        ys, xs = np.nonzero(side)
        for x,y in zip(xs.tolist(), ys.tolist()):
            a, b = (x+start[0],y+start[1]), (x+end[0],y+end[1])
            edges.setdefault(a, []).append((b,direction))
    lengths, holes = [], 0
    while edges:
        first = next(iter(edges))
        point = first
        loop = [first]
        direction = edges[first][0][1]
        while True:
            options = edges[point]
            priority = {1:0, 0:1, 3:2, 2:3}
            index = min(range(len(options)), key=lambda i: priority[(options[i][1]-direction) % 4])
            end, direction = options.pop(index)
            if not options:
                del edges[point]
            point = end
            loop.append(point)
            if point == first:
                break
        points = np.asarray(loop, dtype=float)
        signed = np.sum(points[:-1,0]*points[1:,1]-points[1:,0]*points[:-1,1])
        if signed <= 0:
            holes += 1
            continue
        # Remove the pixel staircase, retaining true corners. Counting raw
        # horizontal/vertical pixel edges would overestimate slanted boundaries.
        split = int(np.argmax(np.sum((points-points[0])**2, axis=1)))
        simple = np.vstack([simplify_open(points[:split+1],2.0)[:-1],
                            simplify_open(points[split:],2.0)])
        lengths.append(float(np.hypot(np.diff(simple[:,0]), np.diff(simple[:,1])).sum()))
    return sum(lengths), len(lengths), holes

radius, count = 40, 96
circle = [(50+radius*math.cos(2*math.pi*i/count),50+radius*math.sin(2*math.pi*i/count)) for i in range(count)]
cases = [
    ('square', [square(0,0,100)], 10000, 400),
    ('donut', [square(0,0,100), square(30,30,40)], 8400, 400),
    ('two-islands', [square(0,0,30),square(70,0,20)], 1300, 200),
    ('island-in-hole', [square(0,0,100),square(20,20,60),square(40,40,20)], 6800, 480),
    ('concave-L', [[(0,0),(100,0),(100,20),(20,20),(20,100),(0,100)]], 3600, 400),
    ('slanted-triangle', [[(0,0),(100,0),(25,70)]], 3500, 100+math.hypot(75,70)+math.hypot(25,70)),
    ('overlapping-parts', [square(0,0,60),square(30,0,60)], 5400, 300),
    ('adjacent-parts', [square(0,0,40),square(40,0,40)], 3200, 240),
    ('duplicate-ring', [square(0,0,100), list(reversed(square(0,0,100)))], 10000, 400),
    ('two-holes', [square(0,0,100),square(10,10,20),square(60,60,20)], 9200, 400),
    ('circle-96-edges', [circle], count/2*radius**2*math.sin(2*math.pi/count), 2*count*radius*math.sin(math.pi/count)),
    ('narrow-polygon', [[(0,0),(100,0),(100,5),(0,5)]], 500, 210),
]
root = Path(tempfile.mkdtemp(prefix='pdf-geometry-', dir=QA_ROOT))
print('OUTPUT', root, flush=True)
project = QgsProject.instance()
registry = QgsApplication.processingRegistry()
provider = DrawingProvider()
registry.addProvider(provider)
algorithm = registry.algorithmById(ALGORITHM_ID)
context = QgsProcessingContext()
context.setProject(project)
poppler = Path(os.environ['PDFTOPPM_EXE'])
results = []
for slug, rings, expected_area, expected_perimeter in cases:
    layer = QgsVectorLayer('Point?crs=EPSG:6678', slug, 'memory')
    layer.dataProvider().addAttributes([QgsField('part', QVariant.Int),QgsField('order',QVariant.Int)])
    layer.updateFields()
    for part, ring in enumerate(rings):
        for order, (x,y) in enumerate(ring):
            feature = QgsFeature(layer.fields())
            feature.setAttributes([part,23+order*3])
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x,y)))
            layer.dataProvider().addFeature(feature)
    layer.updateExtents()
    project.addMapLayer(layer)
    params = {'LAND':layer, 'LAND_NAME':'', 'LAND_PART':'"part"', 'LAND_ORDER':'"order"',
              'LAND_LABEL':'"order"', 'CRS':'EPSG:6678', 'SCALE':1000, 'CREATE_PROJECT':False,
              'SHOW_CALCULATIONS':True, 'OUTPUT':str(root/slug)}
    service = DrawingService(algorithm.request(params,context))
    service.run()
    page = service.role_pages['land'][0]
    assert abs(page.area-expected_area) < 1e-6, (slug,page.area,expected_area)
    assert abs(page.length-expected_perimeter) < 1e-6, (slug,page.length,expected_perimeter)
    filled = page.area_layer.clone()
    filled.setLabelsEnabled(False)
    filled.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({'color':'0,0,255,255','outline_style':'no'})))
    layout = service.create_layout([filled])
    pdf = root / (slug+'.pdf')
    exporter = QgsLayoutExporter(layout)
    settings = QgsLayoutExporter.PdfExportSettings()
    settings.forceVectorOutput = True
    assert exporter.exportToPdf(str(pdf),settings) == QgsLayoutExporter.Success
    dpi = 600
    subprocess.run([str(poppler),'-r',str(dpi),'-png','-singlefile',str(pdf),str(root/slug)],check=True,capture_output=True)
    image = QImage(str(root/(slug+'.png'))).convertToFormat(QImage.Format.Format_RGBA8888)
    buffer = image.constBits()
    buffer.setsize(image.sizeInBytes())
    rgba = np.frombuffer(buffer,dtype=np.uint8).reshape(image.height(),image.bytesPerLine())[:,:image.width()*4].reshape(image.height(),image.width(),4)
    mask = (rgba[:,:,0]<64) & (rgba[:,:,1]<64) & (rgba[:,:,2]>192)
    pixel_count = int(mask.sum())
    metres_per_pixel = 1000 * 0.0254 / dpi
    measured_area = pixel_count * metres_per_pixel**2
    perimeter_pixels, shells, holes = raster_exterior_perimeter(mask)
    measured_perimeter = perimeter_pixels * metres_per_pixel
    area_error = abs(measured_area-expected_area)/expected_area*100
    perimeter_error = abs(measured_perimeter-expected_perimeter)/expected_perimeter*100
    assert area_error <= .5, (slug,'pixel area',measured_area,expected_area,area_error)
    assert perimeter_error <= .5, (slug,'pixel exterior',measured_perimeter,expected_perimeter,perimeter_error)
    results.append({'case':slug,'expected_area':expected_area,'calculated_area':page.area,'pixel_area':measured_area,
                    'expected_perimeter':expected_perimeter,'calculated_perimeter':page.length,'pixel_perimeter':measured_perimeter,
                    'area_error_percent':area_error,'perimeter_error_percent':perimeter_error,'pixels':pixel_count,
                    'exterior_pixel_length':perimeter_pixels,'shells':shells,'holes':holes,'dpi':dpi,'scale':1000,'pdf':str(pdf),
                    'preview':str(root/(slug+'.png')),'html':str(root/slug/'index.html')})
    print(f'{slug}: area={page.area:.3f}/{measured_area:.3f} m2, exterior={page.length:.3f}/{measured_perimeter:.3f} m, errors={area_error:.3f}%/{perimeter_error:.3f}%',flush=True)

# Boundary checks run even in preview and include rotation and padding.
layer = QgsVectorLayer('Point?crs=EPSG:6678','too-large','memory')
for x,y in square(0,0,200):
    f=QgsFeature();f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x,y)));layer.dataProvider().addFeature(f)
layer.updateExtents()
try:
    service.create_layout([layer])
    raise AssertionError('A 200m shape at 1:1000 must not fit in a 150mm frame')
except DrawingError as error:
    assert '3mm' in str(error)

# Pixel checks include labels/notes in the frame margin, beyond geometry alone.
image = QImage(1000,1000,QImage.Format.Format_RGB32)
image.fill(QColor('white'))
from qgis.PyQt.QtGui import QPainter
painter=QPainter(image);painter.fillRect(1,100,10,10,QColor('black'));painter.end()
margin_png=root/'margin-overflow.png';image.save(str(margin_png))
try:
    rendered_map_bounds(margin_png,150,150)
    raise AssertionError('Ink in the 3mm frame padding must fail')
except DrawingError:
    pass
report = {'cases':results,'area_tolerance_percent':.5,'perimeter_tolerance_percent':.5,
          'boundary_simplification_pixels':2,'map_padding_mm':3,'overflow_checks':'passed','output':str(root)}
(root/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
(ROOT/'test/pdf-geometry-latest.txt').write_text(str(root),encoding='utf-8')
print('FRAME OVERFLOW AND PADDING CHECKS OK',flush=True)
print('PASS',flush=True)
os._exit(0)
