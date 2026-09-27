import sys
from pathlib import Path

# The Function App modules (auth_context.py, member_repository.py,
# function_app.py, ...) live one directory up and have no package
# __init__.py, so make sure that directory is importable regardless of
# how pytest's rootdir/import-mode ends up resolving sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
