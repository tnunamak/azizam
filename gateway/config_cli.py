"""Command line for the config sections: ``python proxy.py config get|set|delete|reset|export|import|drift|problems``.

Design 7: the command line calls the HTTP API and nothing else. It never opens admin.db, so every write reaches the
running gateway, which commits it and reloads. It reads only the standard library, so it also runs where the gateway's
dependencies are not installed. Needs the gateway's address (``--url`` or ``AZIZAM_URL``) and an owner key (the file
named by ``--key-file``, else ``AZIZAM_ADMIN_KEY``; a key file keeps the secret out of the process list).

    config get [SECTION [POINTER]]           effective values with their sources
    config set SECTION POINTER JSON          override one entry (JSON text; null is a value)
    config delete SECTION POINTER            hide a release entry (a tombstone)
    config reset SECTION POINTER             go back to inheriting the release value
    config export [--effective] [-o FILE]    the owner's overrides (or the effective view) as JSON
    config import FILE [--mode M] [--apply]  dry run by default; --apply commits what the dry run showed
    config drift | problems                  release changes under overrides | faults found when the config loaded
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence

# (method, url, headers, body) -> (status, parsed JSON or text)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, Any]]
API = "/admin/api/config"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib would copy the Authorization header (the owner key) to the redirect target."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=120) as response:  # noqa: S310 - the operator names the gateway
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace")


class CliError(Exception):
    pass


class Client:
    def __init__(self, url: str, key: str, transport: Transport) -> None:
        self.url, self.key, self.transport = url.rstrip("/"), key, transport

    def call(self, method: str, path: str, body: Any = None, *, headers: dict[str, str] | None = None) -> Any:
        sent = {"Authorization": f"Bearer {self.key}", "Accept": "application/json", **(headers or {})}
        data = None
        if body is not None:
            sent["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        status, payload = self.transport(method, self.url + path, sent, data)
        if 300 <= status < 400:  # the key was sent to this address only; a redirect is never followed
            raise CliError(f"HTTP {status}: the gateway answered with a redirect, which the command does not follow "
                           "(check --url, then retry)")
        if status >= 400 or (status == 202 and isinstance(payload, dict)):
            if status < 400:  # committed, but the reload did not finish: the operator must read it
                print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
                raise CliError(f"HTTP {status}: the change is committed, but the reload failed")
            detail = payload.get("error", {}) if isinstance(payload, dict) else {}
            raise CliError(f"HTTP {status} {detail.get('code', '')}: {detail.get('message', payload)}"
                           + (f"\n{json.dumps(payload.get('problems'), indent=2)}" if isinstance(payload, dict) and payload.get("problems") else ""))
        return payload

    def revision(self, section: str) -> str:
        return str(self.call("GET", f"{API}/sections/{urllib.parse.quote(section, safe='')}")["revision"])


def _show(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _change(client: Client, op: str, section: str, pointer: str, args: argparse.Namespace, value: Any = None) -> None:
    item: dict[str, Any] = {"section": section, "pointer": pointer, "op": op}
    if op == "set":
        item["value"] = value
    body = {"items": [item], "if_match": {section: client.revision(section)}, "note": args.note}
    _show(client.call("PATCH", f"{API}/changes" + ("?dry_run=1" if args.dry_run else ""), body))


def run(args: argparse.Namespace, client: Client) -> None:
    if args.command == "get":
        if not args.section:
            _show(client.call("GET", API))
            return
        view = client.call("GET", f"{API}/sections/{urllib.parse.quote(args.section, safe='')}")
        if args.pointer:
            view = {**view, "entries": [entry for entry in view["entries"] if entry["pointer"] == args.pointer]}
        _show(view)
    elif args.command == "set":
        try:
            value = json.loads(args.value)
        except ValueError as error:
            raise CliError(f"VALUE must be JSON text (quote a string: '\"text\"'): {error}") from error
        _change(client, "set", args.section, args.pointer, args, value)
    elif args.command in ("delete", "reset"):
        _change(client, args.command, args.section, args.pointer, args)
    elif args.command == "export":
        document = client.call("GET", f"{API}/export?format=v1" + ("&view=effective" if args.effective else ""))
        if args.output:
            Path(args.output).write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        else:
            _show(document)
    elif args.command == "import":
        try:
            document = json.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise CliError(f"cannot read {args.file}: {error}") from error
        body = {"document": document, "mode": args.mode, "note": args.note}
        shown = client.call("POST", f"{API}/import?dry_run=1", body)
        _show(shown)
        if args.apply:
            if not shown.get("valid", True):
                raise CliError("the dry run found problems; nothing was applied")
            _show(client.call("POST", f"{API}/import", {**body, "if_match": shown.get("revisions", {})}))
        else:
            print("dry run only; add --apply to commit it", file=sys.stderr)
    elif args.command in ("drift", "problems"):
        _show(client.call("GET", f"{API}/{args.command}"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python proxy.py config", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--url", default=os.environ.get("AZIZAM_URL") or os.environ.get("AZIZAM_BASE_URL"),
                        help="the gateway's address (default: $AZIZAM_URL)")
    parser.add_argument("--key-file", help="file that holds an owner key (default: $AZIZAM_ADMIN_KEY)")
    commands = parser.add_subparsers(dest="command", required=True)
    get = commands.add_parser("get", help="effective values with their sources")
    get.add_argument("section", nargs="?")
    get.add_argument("pointer", nargs="?")
    for name, helptext in (("set", "override one entry"), ("delete", "hide a release entry"), ("reset", "inherit again")):
        sub = commands.add_parser(name, help=helptext)
        sub.add_argument("section")
        sub.add_argument("pointer")
        if name == "set":
            sub.add_argument("value", help="JSON text")
        sub.add_argument("--note")
        sub.add_argument("--dry-run", action="store_true", help="show the diff and write nothing")
    export = commands.add_parser("export", help="the owner's overrides as JSON")
    export.add_argument("--effective", action="store_true", help="the effective config instead (not importable)")
    export.add_argument("-o", "--output")
    imported = commands.add_parser("import", help="import an export file (dry run unless --apply)")
    imported.add_argument("file")
    imported.add_argument("--mode", choices=("replace", "merge"), default="replace")
    imported.add_argument("--apply", action="store_true")
    imported.add_argument("--note")
    commands.add_parser("drift", help="release changes under overrides")
    commands.add_parser("problems", help="faults found when the config loaded")
    return parser


def main(argv: Sequence[str] | None = None, *, transport: Transport = urllib_transport, default_url: str | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    url = args.url or default_url
    key = Path(args.key_file).read_text(encoding="utf-8").strip() if args.key_file else os.environ.get("AZIZAM_ADMIN_KEY", "")
    if not url or not key:
        parser.error("needs the gateway's address (--url or AZIZAM_URL) and an owner key (--key-file or AZIZAM_ADMIN_KEY)")
    try:
        run(args, Client(url, key, transport))
    except CliError as error:
        print(f"config: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
