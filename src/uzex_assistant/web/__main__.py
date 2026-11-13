"""Run the web server:  .venv/bin/python -m uzex_assistant.web"""
import logging
import sys

import uvicorn

from ..bot import RedactFilter
from ..config import get_settings
from ..buildinfo import FrozenBuildMismatch, check_freeze
from ..importer import StaleDatabase, require_database
from .app import create_app

if __name__ == "__main__":
    s = get_settings()
    try:
        require_database(s.data_dir, s.db_path, s.as_of)  # never re-imports at runtime (see UZEX_RUNTIME_IMPORT)
        check_freeze()  # a frozen submission refuses a different build/prompt
    except (StaleDatabase, FrozenBuildMismatch) as e:
        print(f"uzex web: refusing to start: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(RedactFilter())
    uvicorn.run(create_app(s), host=s.web_host, port=s.web_port, log_level="info", proxy_headers=True)
