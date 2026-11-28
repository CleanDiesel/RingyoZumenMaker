"""Create a measurement report archive from the current staged output."""
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from lxml import etree as ET
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import QApplication
from qgis.core import QgsFeature, QgsField, QgsFields, QgsMemoryProviderUtils, QgsVectorFileWriter


def pdf_browser():
    for command in ('msedge', 'google-chrome', 'chromium', 'chromium-browser'):
        executable = shutil.which(command)
        if executable:
            return executable
    for variable in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA'):
        if os.environ.get(variable):
            for suffix in ('Microsoft/Edge/Application/msedge.exe', 'Google/Chrome/Application/chrome.exe'):
                executable = Path(os.environ[variable]) / suffix
                if executable.is_file():
                    return str(executable)
    return None


def print_drawing(owner, browser, source, work, drawing, index):
    root = ET.parse(str(source), ET.HTMLParser()).getroot()
    root.find('head/title').text = '実測図'
    root.find('head').insert(0, ET.Element('base', href=source.parent.resolve().as_uri() + '/'))
    options = root.xpath('//*[@id="zumen_type"]/option')
    if not any(option.get('value') == drawing['panel'] for option in options):
        raise ValueError(f"HTML図面が見つかりません: {drawing['title']}")
    for option in options:
        if option.get('value') == drawing['panel']:
            option.set('selected', 'selected')
        elif option.get('value'):
            option.getparent().remove(option)

    html = work / f'drawing_{index}.html'
    html.write_text('<!DOCTYPE html>\n' + ET.tostring(root, encoding='unicode', method='html'), encoding='utf-8')
    pdf = html.with_suffix('.pdf')
    command = [browser, '--headless', '--disable-gpu', '--no-first-run', '--no-pdf-header-footer',
               '--allow-file-access-from-files', '--disable-crash-reporter', '--disable-breakpad',
               '--disable-background-mode', '--virtual-time-budget=5000',
               f'--user-data-dir={work / ("browser_" + str(index))}', f'--print-to-pdf={pdf}', html.as_uri()]
    with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
        deadline = time.monotonic() + 90
        try:
            while True:
                QApplication.processEvents()
                owner.check_cancelled()
                if pdf.is_file() and pdf.stat().st_size >= 100:
                    with pdf.open('rb') as stream:
                        stream.seek(-32, os.SEEK_END)
                        if b'%%EOF' in stream.read():
                            return pdf
                if process.poll() not in (None, 0):
                    raise OSError(f"HTML図面のPDF化に失敗しました: {drawing['title']}")
                if time.monotonic() >= deadline:
                    raise OSError(f"HTML図面のPDF化がタイムアウトしました: {drawing['title']}")
                time.sleep(.05)
        except BaseException:
            if process.poll() is None:
                process.kill()
            raise


ROLE_LABELS = {'land': '施行地', 'ex_polygon': '除地ポリゴン', 'ex_line': '除地ライン', 'work': '付帯工作物'}
SHAPE_COMPONENTS = {'.shp', '.shx', '.dbf', '.prj', '.cpg'}


def export_shape(owner, drawing, page, destination):
    polygon = page.role in ('land', 'ex_polygon')
    source = page.area_layer if polygon else page.line_layer
    fields = QgsFields()
    for name in ('名称', '区分', '申請No'):
        fields.append(QgsField(name, QVariant.String, len=254))
    if polygon:
        fields.append(QgsField('面積ha', QVariant.Double, len=20, prec=5))
    else:
        fields.append(QgsField('延長m', QVariant.Double, len=20, prec=3))
        fields.append(QgsField('幅m', QVariant.Double, len=20, prec=2))
    layer = QgsMemoryProviderUtils.createMemoryLayer(destination.stem, fields, source.wkbType(), source.crs())
    values = page.values()
    attributes = [page.name_edit.text(), ROLE_LABELS[page.role], '、'.join(drawing['attributes']['application_number'])]
    features = []
    for original in source.getFeatures():
        feature = QgsFeature(fields)
        geometry = original.geometry()
        feature.setGeometry(geometry)
        measurements = [geometry.area() / 10000] if polygon else [geometry.length(), values['haba'] if values['has_haba'] else None]
        feature.setAttributes(attributes + measurements)
        features.append(feature)
    if not features or not layer.dataProvider().addFeatures(features)[0]:
        raise ValueError(f'シェープファイルの地物を準備できません: {destination.stem}')
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = 'ESRI Shapefile'
    options.fileEncoding = 'UTF-8'
    result = QgsVectorFileWriter.writeAsVectorFormatV3(layer, str(destination), owner.render_project.transformContext(), options)
    if result[0] != QgsVectorFileWriter.NoError:
        raise OSError(f'シェープファイルを出力できません: {destination.stem}\n{result[1]}')


def build_report_zip(owner, pending):
    browser = pdf_browser()
    if browser is None:
        raise ValueError('報告ZIPのPDF化にはMicrosoft Edge、Google ChromeまたはChromiumが必要です')
    output = owner.output_dir()
    location = owner.location_pdf_path()
    if not location.is_file():
        raise ValueError('報告ZIPに収録する位置図PDFがありません')
    # Keep browser intermediates separate from the output transaction inventory.
    work = Path(tempfile.mkdtemp(prefix='ringyo_report_')).resolve()
    try:
        with zipfile.ZipFile(pending, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(location, '位置図/位置図.pdf')
            used_names = set()
            for index, drawing in enumerate(owner.role_drawings, 1):
                owner.check_cancelled()
                stem = owner.safe_file_name(drawing['title'])
                name = stem
                suffix = 2
                while name.casefold() in used_names:
                    name = f'{stem} ({suffix})'
                    suffix += 1
                used_names.add(name.casefold())
                pdf = print_drawing(owner, browser, output / 'index.html', work, drawing, index)
                archive.write(pdf, f'図面/{name} - 実測図.pdf')
                shapes = work / f'shapes_{index}'
                shapes.mkdir()
                for page_index, page in enumerate(drawing['pages'], 1):
                    label = ROLE_LABELS[page.role]
                    page_name = page.name_edit.text().strip()
                    filename = owner.safe_file_name(' - '.join(filter(None, (f'{page_index:02d} {label}', page_name))))
                    export_shape(owner, drawing, page, shapes / (filename + '.shp'))
                for part in sorted(shapes.iterdir()):
                    if part.suffix.lower() in SHAPE_COMPONENTS:
                        archive.write(part, f'シェープファイル/{name}/{part.name}')
        with zipfile.ZipFile(pending) as archive:
            if archive.testzip() is not None:
                raise OSError('報告ZIPの検証に失敗しました')
    finally:
        # On Windows, Chromium may release its temporary profile after exiting.
        for _ in range(40):
            shutil.rmtree(work, ignore_errors=True)
            if not work.exists():
                break
            time.sleep(.05)
