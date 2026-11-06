#!/usr/bin/env python3
"""Convert ComfyUI API-format workflows to UI format using the running ComfyUI frontend.

Usage:
  python convert_api_to_ui.py <api1.json> [<api2.json> ...]

For each input, writes a sibling <name>.ui.json by loading the API workflow into
the running ComfyUI frontend at COMFY_URL (default http://127.0.0.1:7890) and
serializing the resulting graph.

Requires playwright (`pip install playwright && playwright install chromium`).
"""
import sys, json, os, asyncio, time
from pathlib import Path

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:7890")

async def main(paths):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(COMFY_URL)
        # Wait until app is initialized
        await page.wait_for_function("typeof window.app !== \"undefined\" && window.app && typeof window.app.loadApiJson === \"function\" && !!window.app.graph", timeout=60000)
        # Allow nodes registry to fully populate
        await asyncio.sleep(2)
        results = []
        for path in paths:
            api_path = Path(path)
            ui_path = api_path.with_name(api_path.stem + ".ui.json")
            try:
                with open(api_path) as f:
                    api_wf = json.load(f)
                # Strip _meta key (proxy-specific, not part of API format)
                api_wf.pop("_meta", None)
                # Load and serialize
                ui = await page.evaluate(
                    """async (apiWorkflow) => {
                        try {
                            await window.app.loadApiJson(apiWorkflow, "tmp.json");
                            await new Promise(r => setTimeout(r, 400));
                            const ui = window.app.graph.serialize();
                            return {ok: true, ui};
                        } catch (e) {
                            return {ok: false, error: String(e), stack: e && e.stack ? e.stack.slice(0, 800) : null};
                        }
                    }""",
                    api_wf,
                )
                if not ui.get("ok"):
                    results.append((api_path.name, "FAIL", ui.get("error")))
                    continue
                with open(ui_path, "w") as f:
                    json.dump(ui["ui"], f, indent=2)
                nc = len(ui["ui"].get("nodes", []))
                results.append((api_path.name, f"OK {nc}n", str(ui_path)))
            except Exception as e:
                results.append((api_path.name, "EXC", str(e)))
        for name, status, info in results:
            print(f"{status:8} {name}  {info or ""}")
        await browser.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    asyncio.run(main(sys.argv[1:]))
