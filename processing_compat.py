"""Compatibility for QGIS batch HTML results, scoped to this provider."""
from functools import wraps
import inspect
import time

ALGORITHM_ID = 'ringyozumenmaker:create_drawings'
_ORIGINAL = '_ringyozumenmaker_original_load_html'


def install_batch_html_compat():
    from qgis.core import QgsProcessingOutputHtml
    from processing.core.ProcessingResults import resultsList
    from processing.gui.BatchAlgorithmDialog import BatchAlgorithmDialog

    if 'timestamp' not in inspect.signature(resultsList.addResult).parameters:
        return
    original = BatchAlgorithmDialog.loadHtmlResults
    if hasattr(original, _ORIGINAL):
        return

    @wraps(original)
    def load_html_results(dialog, results, num):
        if dialog.algorithm().id() != ALGORITHM_ID:
            return original(dialog, results, num)
        for output in dialog.algorithm().outputDefinitions():
            if isinstance(output, QgsProcessingOutputHtml) and results.get(output.name()):
                resultsList.addResult(icon=dialog.algorithm().icon(),
                                      name=f'{output.description()} [{num}]',
                                      timestamp=time.localtime(), result=results[output.name()])

    setattr(load_html_results, _ORIGINAL, original)
    BatchAlgorithmDialog.loadHtmlResults = load_html_results


def remove_batch_html_compat():
    from processing.gui.BatchAlgorithmDialog import BatchAlgorithmDialog
    current = BatchAlgorithmDialog.loadHtmlResults
    original = getattr(current, _ORIGINAL, None)
    if original is not None:
        BatchAlgorithmDialog.loadHtmlResults = original
