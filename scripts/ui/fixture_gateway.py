"""Launch the browser fixture gateway with an enforced connection boundary."""
import sys
from scripts.ui.fixture_guard import install

port, engine_port = map(int, sys.argv[1:])
install({port, engine_port})

import uvicorn

uvicorn.run("proxy:app", host="127.0.0.1", port=port, log_level="warning")
