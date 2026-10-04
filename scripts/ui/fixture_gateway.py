"""Launch the browser fixture gateway with an enforced connection boundary."""
import os
import sys
from scripts.ui.fixture_guard import install, parse_allowed_ports

port, engine_port = map(int, sys.argv[1:])
install({port, engine_port, *parse_allowed_ports(os.environ.get("UI_E2E_ALLOWED_PORTS", ""))})

import uvicorn

uvicorn.run("proxy:app", host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
