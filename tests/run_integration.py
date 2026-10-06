import runpy
import sys
import traceback

try:
    runpy.run_path(sys.argv[1], run_name="__main__")
except BaseException:
    print(traceback.format_exc(), flush=True)
    sys.exit(1)
