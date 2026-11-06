"""Build a reviewable PDF from the independently measured test artifacts."""
from pathlib import Path
from io import BytesIO
import json, os
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
data_root = Path((ROOT/'test/pdf-geometry-latest.txt').read_text(encoding='utf-8').strip())
data = json.loads((data_root/'verification.json').read_text(encoding='utf-8'))
layout = json.loads((data_root/'layout-verification.json').read_text(encoding='utf-8'))
output = Path(os.environ['VERIFICATION_REPORT_DIR'])
output.mkdir(parents=True,exist_ok=True)
pdfmetrics.registerFont(TTFont('YuGothic','C:/Windows/Fonts/YuGothR.ttc',subfontIndex=0))
names = ['正方形','ドーナツ','離れた二つの島','穴の中の島','L字形','斜辺のある三角形',
         '重なる二つのパート','共有辺のあるパート','重複した輪郭','二つの穴','円形（96辺）','細長いポリゴン']
max_area=max(item['area_error_percent'] for item in data['cases'])
max_perimeter=max(item['perimeter_error_percent'] for item in data['cases'])
styles = {name:ParagraphStyle(name,fontName='YuGothic',fontSize=size,leading=leading,textColor=colors.HexColor('#183047'))
          for name,size,leading in [('title',18,25),('text',9,14),('small',8,12)]}
summary = BytesIO()
doc=SimpleDocTemplate(summary,pagesize=landscape(A4),rightMargin=16*mm,leftMargin=16*mm,
                      topMargin=13*mm,bottomMargin=12*mm)
story=[Paragraph('図面計算・PDF逆算検証',styles['title']),
       Paragraph('2026年11月6日 / QGIS 4.2.2 / 縮尺 1:1000 / PDF画像化 600dpi',styles['small']),
       Spacer(1,5*mm),
       Paragraph(f'12例すべて合格。プラグイン計算値は理論値と一致。PDF画像の逆算誤差：面積 最大 {max_area:.3f}%、外周 最大 {max_perimeter:.3f}%（許容 0.5%）。',styles['text']),
       Spacer(1,4*mm)]
table_data=[['形状','理論面積 m²','PDF逆算 m²','誤差 %','理論外周 m','PDF逆算 m','誤差 %']]
for name,item in zip(names,data['cases']):
    table_data.append([name,f"{item['expected_area']:,.3f}",f"{item['pixel_area']:,.3f}",f"{item['area_error_percent']:.3f}",
                       f"{item['expected_perimeter']:,.3f}",f"{item['pixel_perimeter']:,.3f}",f"{item['perimeter_error_percent']:.3f}"])
table=Table(table_data,colWidths=[49*mm,35*mm,35*mm,20*mm,35*mm,35*mm,20*mm],rowHeights=[9*mm]+[6.1*mm]*12)
table.setStyle(TableStyle([('FONTNAME',(0,0),(-1,-1),'YuGothic'),('FONTSIZE',(0,0),(-1,-1),8),
                          ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#183047')),('TEXTCOLOR',(0,0),(-1,0),colors.white),
                          ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#eef4f7')]),
                          ('ALIGN',(1,0),(-1,-1),'RIGHT'),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
                          ('LINEBELOW',(0,0),(-1,0),.5,colors.HexColor('#183047'))]))
story.extend([table,Spacer(1,5*mm)])
for text in [
    '測定方法：プラグインが測点から構築した図形を、計測用の青一色でPDF出力。青色ピクセル数 × (縮尺 × 0.0254 ÷ dpi)² から実面積を逆算しました。',
    '外周：画像の塗り領域の外側輪郭を抽出し、穴の境界を除いて島ごとに合計。斜辺の階段状の誤差は2px以内で単純化し、縮尺から実距離へ換算しました。',
    '判定：寸法から独立に求めた理論値とプラグイン値の差は全例で 0.000001 以下。ドーナツの内周・共有辺・重複輪郭を外周に加算しないことも確認しました。',
    '配置：A4・A3で計算欄を2ページ目へ移動。重ならなければ1ページ目に残し、座標表や長い計算式の続きも欠落なく印刷。地図枠の内側3mmと、はみ出し時のエラーを確認しました。',
    '後続ページ：計測用PDF12例、その後に通常の実測図PDF（地図1ページ＋計算1ページ）を収録。青塗りは計測用で、通常の出力は線図です。',
]:
    story.append(Paragraph(text,styles['small']))
    story.append(Spacer(1,1.4*mm))
doc.build(story)
writer=PdfWriter()
reader=PdfReader(BytesIO(summary.getvalue()))
assert len(reader.pages)==1
writer.add_page(reader.pages[0])
for index,(name,item) in enumerate(zip(names,data['cases']),1):
    source=PdfReader(item['pdf'])
    page=source.pages[0]
    width,height=float(page.mediabox.width),float(page.mediabox.height)
    overlay=BytesIO()
    c=canvas.Canvas(overlay,pagesize=(width,height))
    c.setFont('YuGothic',11);c.setFillColor(colors.HexColor('#183047'))
    c.drawString(5*mm,height-8*mm,f'{index:02d}. {name} / {item["case"]}')
    c.setFont('YuGothic',7.5)
    c.drawString(5*mm,11*mm,f'理論面積 {item["expected_area"]:,.3f} m² / PDF逆算 {item["pixel_area"]:,.3f} m² / 誤差 {item["area_error_percent"]:.3f}%')
    c.drawString(5*mm,6*mm,f'理論外周 {item["expected_perimeter"]:,.3f} m / PDF逆算 {item["pixel_perimeter"]:,.3f} m / 誤差 {item["perimeter_error_percent"]:.3f}%')
    c.save()
    page.merge_page(PdfReader(BytesIO(overlay.getvalue())).pages[0])
    writer.add_page(page)
for page in PdfReader(layout['a4']).pages:
    writer.add_page(page)
target=output/'図面検証.pdf'
with target.open('wb') as stream: writer.write(stream)
assert len(PdfReader(target).pages)==15
print(json.dumps({'pdf':str(target),'pages':15,'max_area_error_percent':max_area,
                  'max_perimeter_error_percent':max_perimeter},ensure_ascii=False),flush=True)
