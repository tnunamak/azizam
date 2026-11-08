import json, os
import proxy

def test_dump():
    proxy.app.openapi_schema = None
    json.dump(proxy.app.openapi(), open(os.path.expanduser("~/.tmp/c2d/b/new_openapi.json"), "w"), sort_keys=True, default=str)
