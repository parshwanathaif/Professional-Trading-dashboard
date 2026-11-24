"""Entry point: `streamlit run dashboard.py` (set BQL_ROOT to use another workspace, e.g. demo_workspace)."""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "dashboard" / "app.py"), run_name="__main__")
