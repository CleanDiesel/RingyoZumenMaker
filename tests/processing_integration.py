"""Exercise native Processing, copied commands, and real batch autofill widgets."""
from pathlib import Path
import os, sys, tempfile, json, copy
ROOT=Path(__file__).resolve().parents[1]
bootstrap=(ROOT/'tests/measurement_integration.py').read_text(encoding='utf-8').split('from RingyoZumenMaker.main import Main')[0]
exec(bootstrap)
from RingyoZumenMaker.processing_provider import DrawingProvider, ALGORITHM_ID
from RingyoZumenMaker.drawing_service import DrawingService
from RingyoZumenMaker.drawing_inputs import DrawingError, DrawingRequest, DrawingCancelled
assert 'RingyoZumenMaker.main' not in sys.modules
registry=QgsApplication.processingRegistry()
provider=DrawingProvider()
assert registry.addProvider(provider)
algorithm=registry.algorithmById(ALGORITHM_ID)
assert algorithm is not None
assert algorithm.flags() & Qgis.ProcessingAlgorithmFlag.SupportsBatch
assert algorithm.flags() & Qgis.ProcessingAlgorithmFlag.NoThreading
print('PROVIDER REGISTRATION AND BATCH FLAGS OK', flush=True)

project=QgsProject.instance()
def points(name, items):
    layer=QgsVectorLayer('Point?crs=EPSG:6678',name,'memory')
    layer.dataProvider().addAttributes([QgsField('number',QVariant.Int),QgsField('name',QVariant.String),QgsField('section',QVariant.String)])
    layer.updateFields()
    for number,(x,y,group) in enumerate(items,1):
        feature=QgsFeature(layer.fields());feature.setAttributes([number,'No.'+str(number),group]);feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x,y)))
        layer.dataProvider().addFeature(feature)
    layer.updateExtents();project.addMapLayer(layer);return layer
land=points('施行地点',[(0,0,'A'),(100,0,'A'),(100,100,'A'),(0,100,'A')])
work=points('工作物点',[(0,10,'排水1'),(80,10,'排水1'),(10,20,'排水2'),(90,20,'排水2')])
root=Path(tempfile.mkdtemp(prefix='processing-drawings-',dir=QA_ROOT))
print('OUTPUT',root,flush=True)
context=QgsProcessingContext();context.setProject(project)
feedback=QgsProcessingFeedback()
parameters={'LAND':land,'WORK':work,'LAND_LABEL':'"name"','LAND_ORDER':'"number"',
            'WORK_GROUP':'"section"','WORK_LABEL':'"name"','WORK_ORDER':'"number"',
            'LAND_FOREST_OWNER':'施行地所有者','WORK_FOREST_OWNER':'工作物所有者',
            'LAND_FOREST_COMPARTMENT':'1林班2小班','CRS':'EPSG:6678','TABLE_ORDER':1,'CREATE_PROJECT':True,'OUTPUT':str(root/'single')}
before=set(project.mapLayers()); dirty=project.isDirty();project_name=project.fileName()
import processing
result=processing.run(ALGORITHM_ID,parameters,context=context,feedback=feedback)
assert Path(result['HTML']).is_file() and Path(result['PROJECT']).is_file()
assert 'RingyoZumenMaker.main' not in sys.modules, 'The calculation service must not create/import an input dock'
assert set(project.mapLayers())==before and project.isDirty()==dirty and project.fileName()==project_name, (before,set(project.mapLayers()),dirty,project.isDirty(),project_name,project.fileName())
config=json.loads(Path(result['CONFIG']).read_text(encoding='utf-8'))
assert len(config['polygons']['items'])==1 and len(config['lines']['items'])==2
assert all(i['drawing_attributes']['forest_owner']=='工作物所有者' for i in config['lines']['items'])
print('NATIVE PROCESSING WITHOUT AN INPUT WINDOW OK',flush=True)

command_params=dict(parameters,OUTPUT=str(root/'copied'))
command=algorithm.asPythonCommand(command_params,context)
assert ALGORITHM_ID in command and 'WORK_FOREST_OWNER' in command
namespace={'processing':processing}
exec('copied_result = '+command,namespace)
assert Path(namespace['copied_result']['HTML']).is_file()
print('COPIED PYTHON COMMAND EXECUTION OK',flush=True)

from processing.gui.BatchAlgorithmDialog import BatchAlgorithmDialog
dialog=BatchAlgorithmDialog(algorithm.create())
panel=dialog.mainWidget()
panel.addRow()
while panel.batchRowCount()>2:
    raise AssertionError('Unexpected initial batch row count')
assert panel.batchRowCount()==2
for name,value in parameters.items():
    column=panel.parameter_to_column[name]
    filler=panel.tblParameters.cellWidget(0,column)
    filler.setRowValue(0,value,context)
    filler.fillDown()
for row in range(2):
    assert panel.valueForParameter(row,'WORK_FOREST_OWNER')=='工作物所有者'
    assert panel.valueForParameter(row,'TABLE_ORDER')==1
    column=panel.parameter_to_column['OUTPUT']
    panel.tblParameters.cellWidget(0,column).setRowValue(row,str(root/('batch-'+str(row+1))),context)
    owner_column=panel.parameter_to_column['LAND_FOREST_OWNER']
    panel.tblParameters.cellWidget(0,owner_column).setRowValue(row,'現場所有者'+str(row+1),context)
    row_parameters,ok=panel.parametersForRow(row,context,warnOnInvalid=True)
    assert ok,row_parameters
    batch_result=processing.run(ALGORITHM_ID,row_parameters,context=context)
    assert Path(batch_result['HTML']).is_file()
    assert '現場所有者'+str(row+1) in Path(batch_result['HTML']).read_text(encoding='utf-8')
dialog.resize(1500,750);dialog.show();app.processEvents();dialog.grab().save(str(root/'batch-autofill.png'));dialog.hide()
print('ACTUAL BATCH AUTOFILL AND TWO-ROW EXPORT OK',flush=True)

cancelled=QgsProcessingFeedback();cancelled.cancel()
try:
    processing.run(ALGORITHM_ID,dict(parameters,OUTPUT=str(root/'cancelled')),context=context,feedback=cancelled)
    raise AssertionError('Cancelled job must fail')
except QgsProcessingException:
    assert not (root/'cancelled'/'index.html').exists()
print('CANCELLATION OK',flush=True)

# Dock copy retains multiple inputs with independent metadata in one command.
from RingyoZumenMaker.main import Main
from RingyoZumenMaker.processing_provider import parameters_from_dock
dock=Main();dock.crs.setCrs(QgsCoordinateReferenceSystem('EPSG:6678'))
dock.scale.setScale(1000)
dock.fileName.setFilePath(str(root/'dock-copy'))
for owner in ('個別所有者A','個別所有者B'):
    page=dock.add_role_page('work');page.point_layer.setLayer(work)
    page.filter_exp.setExpression('"section" = '+QgsExpression.quotedString('排水1'))
    page.sokuten_labels.set_expressions(['"name"'],0);page.sort_exp.setExpression('"number"')
    page.attribute_inputs['forest_owner'].setText(owner)
copied=parameters_from_dock(dock)
command=algorithm.asPythonCommand(copied,context)
exec('copied_result = '+command,namespace)
html=Path(namespace['copied_result']['HTML']).read_text(encoding='utf-8')
assert '個別所有者A' in html and '個別所有者B' in html
from qgis.PyQt.QtWidgets import QApplication
action=next(a for a in dock.processing_button.menu().actions() if a.text()=='Pythonコマンドをコピー')
action.trigger()
assert ALGORITHM_ID in QApplication.clipboard().text()
print('DOCK COPY WITH INDEPENDENT DRAWING ATTRIBUTES OK',flush=True)
# A single registration maps directly to the visible Processing columns.
dock.remove_role_page(dock.role_pages['work'][1])
simple=parameters_from_dock(dock)
assert 'DOCK_INPUTS' not in simple
assert simple['WORK_FOREST_OWNER']=='個別所有者A'
assert simple['WORK_FILTER']
simple['OUTPUT']=str(root/'dock-simple')
simple_result=processing.run(ALGORITHM_ID,simple,context=context)
assert '個別所有者A' in Path(simple_result['HTML']).read_text(encoding='utf-8')
print('SINGLE DOCK INPUT MAPS TO VISIBLE COLUMNS OK',flush=True)
project.clear();registry.removeProvider(provider)
from RingyoZumenMaker.plugin import RingyoZumenMakerPlugin
plugin=RingyoZumenMakerPlugin(None)
plugin.initProcessing();plugin.initProcessing()
assert registry.algorithmById(ALGORITHM_ID) is not None
plugin.unload()
assert registry.providerById('ringyozumenmaker') is None
print('PROVIDER LOAD AND UNLOAD LIFECYCLE OK',flush=True)
print('PASS',flush=True)
os._exit(0)
