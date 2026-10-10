"""Project restoration, rendering contexts and editable-project snapshots."""
import copy
from contextlib import contextmanager
from pathlib import Path

from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import QgsProject, QgsVectorFileWriter

from .unified_project import metadata


class ProjectSession:
    @contextmanager
    def export_layer_context(self, layout):
        """Resolve cross-layer styles without replacing QGIS's singleton."""
        live = QgsProject.instance()
        project = layout.project()
        temporary, saved_maps = [], []
        dirty = live.isDirty()
        try:
            if project is not live:
                replacements = {}
                for layer in project.mapLayers().values():
                    if live.mapLayer(layer.id()) is None and hasattr(layer, "getFeatures"):
                        clone = layer.clone()
                        live.addMapLayer(clone, False)
                        temporary.append(clone.id())
                        replacements[layer.id()] = clone.id()
                from qgis.core import QgsLayoutItemMap
                for item in layout.items():
                    if not isinstance(item, QgsLayoutItemMap) or not item.followVisibilityPreset():
                        continue
                    saved_maps.append((item, item.layers(), item.layerStyleOverrides(), item.keepLayerSet(), item.keepLayerStyles()))
                    styles = project.mapThemeCollection().mapThemeStyleOverrides(item.followVisibilityPresetName())
                    for layer_id, xml in styles.items():
                        for original, replacement in replacements.items():
                            xml = xml.replace(original, replacement)
                        styles[layer_id] = xml
                    item.setLayers(item.layersToRender())
                    item.setFollowVisibilityPreset(False)
                    item.setKeepLayerSet(True)
                    item.setLayerStyleOverrides(styles)
                    item.setKeepLayerStyles(True)
            yield
        finally:
            for item, layers, styles, keep_set, keep_styles in saved_maps:
                item.setLayers(layers)
                item.setLayerStyleOverrides(styles)
                item.setKeepLayerSet(keep_set)
                item.setKeepLayerStyles(keep_styles)
                item.setFollowVisibilityPreset(True)
            live.removeMapLayers(temporary)
            live.setDirty(dirty)

    def layout_display_scale(self, layout):
        from qgis.core import QgsLayoutItemMap
        maps = [item for item in layout.items() if isinstance(item, QgsLayoutItemMap)]
        if not maps:
            raise ValueError("レイアウトに地図がありません")
        item = max(maps, key=lambda item: item.sizeWithUnits().width() * item.sizeWithUnits().height())
        return item.scale() * layout.pageCollection().page(0).pageSize().width() / (150 * self.paper_factor())

    def restore_project_settings(self, *_):
        project = QgsProject.instance()
        info = metadata(project)
        if not info:
            return
        config = copy.deepcopy(info["config"])
        base = Path(project.fileName()).parent
        config["output"]["directory"] = str((base / config["output"]["directory"]).resolve())
        for name in ("assignment_ortho", "config_file"):
            value = config["output"].get(name)
            if value and not Path(value).is_absolute():
                config["output"][name] = str((base / value).resolve())
        self.apply_configuration(config)

    def settle_project_edits(self):
        project = QgsProject.instance()
        layers = [layer for layer in project.mapLayers().values()
                  if hasattr(layer, "isModified") and layer.isModified()]
        if not layers:
            return True
        result = QMessageBox.question(self, "未保存の地物編集",
            "地物の編集が未保存です。保存しますか？\n" + "\n".join(layer.name() for layer in layers),
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if result == QMessageBox.StandardButton.Cancel:
            return False
        for layer in layers:
            if result == QMessageBox.StandardButton.Save:
                if not layer.commitChanges():
                    raise OSError("地物編集を保存できません: " + layer.name() + "\n" + "\n".join(layer.commitErrors()))
            elif not layer.rollBack():
                raise OSError("地物編集を取り消せません: " + layer.name())
        return True

    def snapshot_project(self, path):
        project = QgsProject.instance()
        if not metadata(project):
            return None
        filename, dirty = project.fileName(), project.isDirty()
        try:
            if not project.write(str(path)):
                raise OSError("現在のプロジェクトを一時保存できません")
        finally:
            project.setFileName(filename)
            project.setDirty(dirty)
        snapshot = QgsProject()
        if not snapshot.read(str(path)):
            raise OSError("現在のプロジェクトの保存結果を検証できません")
        managed = set(metadata(project).get("datasets", {}).values())
        for layer in project.mapLayers().values():
            if layer.providerType() != "memory" or layer.id() in managed:
                continue
            # User-added scratch data belong to the editable QGZ, not the managed GPKG.
            saved = snapshot.mapLayer(layer.id())
            if saved is None:
                continue
            attached = snapshot.createAttachedFile(layer.id() + ".gpkg")
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = "scratch"
            result = QgsVectorFileWriter.writeAsVectorFormatV3(layer, attached, project.transformContext(), options)
            if result[0] != QgsVectorFileWriter.NoError:
                raise OSError("追加したスクラッチレイヤを保存できません: " + layer.name())
            from qgis.core import QgsMapLayerStyle
            style = QgsMapLayerStyle()
            style.readFromLayer(layer)
            saved.setDataSource(attached + "|layername=scratch", layer.name(), "ogr")
            style.writeToLayer(saved)
        if not snapshot.write():
            raise OSError("編集済みプロジェクトを保存できません")
        snapshot.clear()
        return path
