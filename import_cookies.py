#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import Flow/Google cookies you already have, and emit the env value.

Two ways to use it:

  A) From a browser DevTools export (Application -> Cookies, or a
     "Cookie-Editor" JSON export from your phone):
       python import_cookies.py devtools-cookies.json

  B) From a raw Cookie header string (what DevTools "Copy as cURL" shows):
       python import_cookies.py --header "SID=...; HSID=...; SSID=...; ..."

It writes sessions/flow.cookies.full.json and prints a base64 blob you can
paste straight into the Render env var FLOW_COOKIES_JSON (handy from a phone).

You need the cookies that identify the signed-in Google account for
flow.google.com — at minimum the `SID/HSID/SSID/APISID/SAPISID` family plus
`__Secure-1PSID` / `__Secure-3PSID` and `__Secure-next-auth.session-token`.
"""
import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "sessions" / "flow.cookies.full.json"


def from_devtools(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    # Cookie-Editor export is a flat list; DevTools "Copy all" is TSV text.
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    out = []
    for c in data:
        if not isinstance(c, dict) or "name" not in c:
            continue
        out.append({
            "name": c["name"],
            "value": c.get("value", ""),
            "domain": c.get("domain") or ".google.com",
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
            "expirationDate": c.get("expirationDate") or c.get("expires"),
        })
    return out


def from_header(hdr):
    out = []
    for part in hdr.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out.append({"name": k.strip(), "value": v.strip(),
                    "domain": ".google.com", "path": "/", "secure": True})
    return out


def main():
    ap = argparse.ArgumentParser(description="Import Google/Flow cookies")
    ap.add_argument("file", nargs="?", help="devtools/Cookie-Editor JSON export")
    ap.add_argument("--header", default="", help="raw 'name=value; ...' string")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    if a.header:
        jar = from_header(a.header)
    elif a.file:
        jar = from_devtools(a.file)
    else:
        ap.print_help()
        return 1

    if not jar:
        print("[!] no cookies parsed")
        return 1
    dest = Path(a.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(jar, indent=2), encoding="utf-8")
    print(f"[*] wrote {len(jar)} cookies -> {dest}")
    blob = base64.b64encode(json.dumps(jar).encode()).decode()
    print("\nFLOW_COOKIES_JSON (paste into Render env, base64):\n")
    print(blob)
    print(f"\n(length {len(blob)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())