"""Make bundled file references relative in the current QGZ export."""
from pathlib import Path
import os
import re
import tempfile
import zipfile


def make_project_references_relative(project_path, source_paths):
    """Keep selected colocated files relative in an absolute-path QGZ."""
    project_path = Path(project_path)
    replacement_groups = []
    for source_path in source_paths:
        source_path = Path(source_path)
        if not source_path.exists():
            continue
        resolved = source_path.resolve()
        relative_path = Path(os.path.relpath(source_path, project_path.parent)).as_posix()
        if not relative_path.startswith("."):
            relative_path = "./" + relative_path
        replacement_groups.append((
            source_path.name,
            {str(resolved), resolved.as_posix()},
            relative_path,
        ))

    with zipfile.ZipFile(project_path, "r") as source:
        entries = [(info, source.read(info.filename)) for info in source.infolist()]

    replaced_names = set()
    rewritten = []
    for info, data in entries:
        if info.filename.lower().endswith(".qgs"):
            text = data.decode("utf-8")
            # QGISの読込側にも相対パスを解決させる。外部の絶対パスは保持。
            text = re.sub(r'(<properties name="Absolute" type="bool">)true(</properties>)',
                          r'\g<1>false\g<2>', text)
            text = re.sub(r'(<Absolute type="bool">)true(</Absolute>)',
                          r'\g<1>false\g<2>', text)
            for source_name, absolute_paths, relative_path in replacement_groups:
                for absolute_path in absolute_paths:
                    if absolute_path in text:
                        text = text.replace(absolute_path, relative_path)
                        replaced_names.add(source_name)
            data = text.encode("utf-8")
        rewritten.append((info, data))

    expected_names = {group[0] for group in replacement_groups}
    missing_names = sorted(expected_names - replaced_names)
    if missing_names:
        raise RuntimeError(
            "QGZ内の同梱ファイル参照を相対化できません: "
            + ", ".join(missing_names)
        )

    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{project_path.stem}_",
        suffix=".qgz",
        dir=project_path.parent,
    )
    os.close(handle)
    try:
        with zipfile.ZipFile(temporary_name, "w") as target:
            for info, data in rewritten:
                target.writestr(info, data)
        os.replace(temporary_name, project_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


class EditableProjects:
    """Capture measurement maps into one project with persistent page identities."""
    def __init__(self, owner, directory, snapshot=None):
        from .unified_project import UnifiedProjects
        self.owner = owner
        self.engine = UnifiedProjects(owner, directory, snapshot=snapshot)

    def capture(self, layout, name, output, persistent_layers=()):
        from qgis.core import QgsLayoutItemMap, QgsMapLayerStyle, QgsVectorLayer, QgsField
        from qgis.PyQt.QtCore import QVariant
        from .unified_project import UID
        import uuid
        key = name
        for prefix, pages in (("shui", self.owner.shuis), ("haisui", self.owner.haisuis)):
            for page in pages:
                if name == f"{prefix}_{page.index}":
                    key = prefix + ":" + page.drawing_uid
        layers = []
        for item in layout.items():
            if not isinstance(item, QgsLayoutItemMap):
                continue
            for index, layer in enumerate(item.layers()):
                dataset_key = "background:" + layer.id()
                if layer in persistent_layers or (name != "location" and isinstance(layer, QgsVectorLayer)):
                    dataset_key = "output:" + key + ":" + str(index)
                    for prefix, pages in (("shui", self.owner.shuis), ("haisui", self.owner.haisuis)):
                        for page in pages:
                            for attr in ("pt_layer", "line_layer", "area_layer"):
                                if layer is getattr(page, attr, None):
                                    dataset_key = f"output:{prefix}:{page.drawing_uid}:{attr}"
                    if UID not in layer.fields().names():
                        layer.dataProvider().addAttributes([QgsField(UID, QVariant.String, len=36)])
                        layer.updateFields()
                        uid_index = layer.fields().indexFromName(UID)
                        layer.dataProvider().changeAttributeValues({f.id(): {uid_index: str(uuid.uuid5(uuid.NAMESPACE_URL, dataset_key + ":" + str(f.id())))} for f in layer.getFeatures()})
                self.engine.add_dataset(dataset_key, layer)
                style = QgsMapLayerStyle()
                style.readFromLayer(layer)
                layers.append((layer.id(), dataset_key, style))
        drawing = next((d for d in getattr(self.owner, "role_drawings", []) if d["panel"] == name), None)
        title = drawing["title"] if drawing else "位置図" if name == "location" else name
        self.engine.capture(layout, key, title, output, layers, {"folder": name, "title": title})

    def save(self):
        project, info = self.engine.save()
        self.owner._generated_scales = {}
        from qgis.core import QgsLayoutExporter, QgsLayoutItemMap
        with self.owner.export_layer_context_for_project(project):
            for descriptor in info["layouts"]:
                layout = project.layoutManager().layoutByName(descriptor["layout"])
                target = self.owner.output_dir() / descriptor["output"]
                target.parent.mkdir(parents=True, exist_ok=True)
                exporter = QgsLayoutExporter(layout)
                if target.suffix == ".pdf":
                    settings = QgsLayoutExporter.PdfExportSettings()
                    result = exporter.exportToPdf(str(target), settings)
                else:
                    settings = QgsLayoutExporter.ImageExportSettings()
                    settings.dpi = 300
                    result = exporter.exportToImage(str(target), settings)
                    maps = [i for i in layout.items() if isinstance(i, QgsLayoutItemMap)]
                    if maps:
                        maps = [item for item in maps if item.customProperty("rz/generated_map", False)] or maps
                        main = max(maps, key=lambda item: item.sizeWithUnits().width() * item.sizeWithUnits().height())
                        self.owner._generated_scales[descriptor["drawing"]["folder"]] = main.scale()
                if result != QgsLayoutExporter.Success:
                    raise OSError("編集済み図面を書き出せません: " + str(target))
                if target.suffix.lower() == '.png' and descriptor.get('drawing'):
                    from .map_frame import rendered_map_bounds
                    panel = descriptor['drawing']['folder']
                    self.owner._map_content_bounds[panel] = rendered_map_bounds(
                        target, 150 * self.owner.paper_factor(), 150 * self.owner.paper_factor(),
                        descriptor['drawing'].get('title', layout.name()))
        text = (Path(__file__).parent / "qgs_editing.md").read_text(encoding="utf-8")
        text += "\n## 今回のレイアウトと出力先\n\n"
        for item in info["layouts"]:
            text += f'- {item["layout"]}: `{item["output"]}`\n'
        (self.engine.qgz_dir / "操作説明.md").write_text(text, encoding="utf-8")
        self.owner._generated_project_path = self.engine.project_path
        project.clear()
        self.engine.old_layers.clear()
