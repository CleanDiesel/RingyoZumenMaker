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
