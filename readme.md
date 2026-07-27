# RingyoZumenMaker

QGIS上のポイントレイヤから、林業向けの実測図、位置図を作成するプラグインです。地図画像、座標表、測量情報などをまとめたHTML図面を出力します。

[図面のサンプルはこちら](sample/)

## 仕様

北海道庁の[造林事業に係る造林実測図の作成方法](https://www.pref.hokkaido.lg.jp/fs/1/3/1/2/9/9/9/9/_/%E9%80%A0%E6%9E%97%E4%BA%8B%E6%A5%AD%E3%81%AB%E4%BF%82%E3%82%8B%E9%80%A0%E6%9E%97%E5%AE%9F%E6%B8%AC%E5%9B%B3%E3%81%AE%E4%BD%9C%E6%88%90%E6%96%B9%E6%B3%95.pdf)に基づいて作成しています。

## 動作環境

- QGIS 4.0以降
- Python 3.12以降（QGIS同梱環境）
- Pythonパッケージ `lxml`

動作確認環境: QGIS 4.0.1 Norrköping

## 主な機能

- 周囲ポイントから周囲線・面積・延長を計算
- 排水・林内路網を複数登録し、線形と延長を計算
- 排水・林内路網を除地として扱い、除地延長・除地面積・差引面積を計算
- 測点名をQGIS式で指定
- QGIS式による対象ポイントの絞り込みと結合順の指定
- 指定した縮尺、CRS、磁補正角を地図へ反映
- 座標表示の小数点以下桁数を指定
- 周囲図、排水・林内路網図、全体図を含むHTML図面を生成
- 位置図PDFを生成
- 作成した点・線レイヤをGeoPackageとして保存
- 試算モードで面積等の計算結果を事前確認

## インストール

### gitを使用する場合

1. プラグインフォルダ内で`git clone`します。

例：

```powershell
# Windows
cd $env:APPDATA\QGIS\QGIS4\profiles\default\python\plugins
git clone https://github.com/CleanDiesel/RingyoZumenMaker.git
```

```bash
# Linux
cd ~/.local/share/QGIS/QGIS4/profiles/default/python/plugins
git clone https://github.com/CleanDiesel/RingyoZumenMaker.git
```

### zipからインストールする場合

1. [Releases](../../releases/latest)から最新版のzipをダウンロードします。
2. QGISを起動します。
3. 「プラグイン」タブの「プラグインの管理とインストール」を開きます。
4. 「ZIPからインストール」でダウンロードした`RingyoZumenMaker-(version).zip`を読み込みます。

## 使用方法

### 1. 基本情報

「基本情報」タブで、各種項目を設定します。

### 2. 座標系

「座標系」タブで出力図面のCRSと磁補正角度を指定します。

長さと面積は、指定したCRSへ再投影したレイヤから計算します。緯度経度などの地理座標系やメートル単位ではないCRSは使用できません。現場の地域に合った平面直角座標系などを指定してください。

推奨CRS：JGD2011 / Japan Plane Rectangular CS I\~XIX または JGD 2024 Japan Zone 1\~19

### 3. 周囲実測図

周囲実測図を出力する場合は「周囲の図面を出力」を有効にし、次の項目を指定します。

- ポイントレイヤ
- 対象を絞り込むQGIS式（空欄の場合はレイヤ全体）

```qgis
# 選択中のポイントだけを対象にする
is_selected()

# 属性 field に「周囲」が入っているポイントだけを対象にする
"type" = '周囲'
```

- 測点名のQGIS式

```qgis
# 測点名を name フィールドから表示する
"name"

# 測点名を「No.1」「No.2」のように表示する
'No.' || "number"
```

- ポイントの結合順を決めるQGIS式

```qgis
# number フィールドの昇順でポイントを結合する
"number"

# name フィールドを自然順で結合する
"name"
```

### 4. 排水・林内路網実測図

排水・林内路網実測図を出力する場合は「排水・林内路網の図面を出力」を有効にします。「排水を追加」で入力欄を増やし、それぞれに名前、幅、ポイントレイヤ、測点名、結合順などを指定します。

各排水・林内路網は「除地として扱う」を選択できます。除地として扱う場合、幅と延長から除地面積を計算し、周囲面積との差引面積をHTML図面へ表示します。100m2未満の除地面積は、計算上0m2として扱います。

### 5. 出力

「出力」タブで既存の出力フォルダを指定します。

- 「試算」: 面積、周長等の計算結果を確認します。地図PNG、位置図PDF、GeoPackageは書き込みません。
- 「実行」: 実測図HTML、必要に応じて位置図PDF、GeoPackageを書き込みます。

## 出力ファイル

指定フォルダへ次のファイルを生成します。

```text
出力フォルダ/
├── index.html
├── ringyo_zumen.gpkg
├── 林小班名 - 位置図.pdf
└── asset/
    ├── index.css
    ├── houi.svg
    ├── shui_map.png
    ├── haisui_1_map.png
    ├── haisui_2_map.png
    └── mix_map.png
```

選択した製図種別、位置図の生成設定、排水・林内路網の登録数によって、生成されるPNGファイルやPDFは異なります。`index.html`をWebブラウザで開くと図面を確認できます。
印刷する場合ブラウザの印刷機能を使用します。ページ上のセレクトボックス、ボタンなどは印刷時に非表示になります。

`ringyo_zumen.gpkg`には、作成した周囲の点・線レイヤ、排水・林内路網の点・線レイヤが保存されます。

## 注意

- 同じ出力フォルダを使用すると、同名のHTML、画像、アセットが上書きされます。
- `ringyo_zumen.gpkg`も同じ出力フォルダ内で上書きされます。

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
