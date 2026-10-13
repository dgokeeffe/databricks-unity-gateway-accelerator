"""Databricks Apps entrypoint for the accelerator's public demo/operator UI."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
    uvicorn.run("aigw_accelerator.webapp:app", host="0.0.0.0", port=port)
