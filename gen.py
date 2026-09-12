#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flow one-shot CLI — generate images from anywhere with one command.

Usage:
  python gen.py "a red fox in snow"            generate (auto-downloads)
  python gen.py "prompt" -m HARBOR_SEAL -s 777 --upscale 2K
  python gen.py --history [--json]             list media (no browser needed)
  python gen.py --credits [--json]             credits (no browser needed)
  python gen.py --download <uuid>              re-download (boots engine)
  python gen.py --status                       health (no browser needed)
  python gen.py --project <id>                 switch project

Exit codes: 0 ok · 1 usage/config error · 2 operation failed.
Needs: an authenticated profile (see README §5) + pip install -r requirements.txt
Config via env or .env: FLOW_PROFILE FLOW_OUT FLOW_PORT FLOW_CHROME
                        FLOW_PROJECT FLOW_MODEL FLOW_EMAIL FLOW_PASSWORD
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import flow_api
from flow_api import FlowEngine, PureHTTP, load_state, save_state, ASPECTS


def emit(obj, as_json):
    if as_json:
        print(json.dumps(obj, default=str))
    else:
        print(obj)


def fail(msg, as_json, code=2):
    if as_json:
        print(json.dumps({"ok": False, "error": msg}))
    else:
        print(f"[!] {msg}")
    return code


def cmd_status(a):
    http = PureHTTP()
    st = load_state()
    pid = st.get("project_id", "")
    sess = bool(pid) and http.history(pid) is not None
    out = {"ok": True, "project": pid, "model": st.get("model", "NARWHAL"),
           "session_ok": sess, "engine": "not started (pure HTTP)"}
    emit(out if a.json else f"project={pid or '-'} model={out['model']} "
                            f"session={'ok' if sess else 'DEAD'}", a.json)
    return 0 if sess else 2


def cmd_credits(a):
    c = PureHTTP().credits()
    if c is None:
        return fail("credits failed (stale session? sign in once)", a.json)
    emit(c if a.json else f"credits: {c.get('credits')}", a.json)
    return 0


def cmd_history(a):
    st = load_state()
    items = PureHTTP().history(st.get("project_id", ""))
    if items is None:
        return fail("history failed (stale session? sign in once)", a.json)
    if a.json:
        emit({"ok": True, "media": items}, True)
    else:
        print(f"{len(items)} items:")
        for d in items[:30]:
            print(f"  {d['uuid'][:13]}.. {str(d['prompt'])[:45]!r} "
                  f"seed={d['seed']} {d['width']}x{d['height']} {d['size']}B")
    return 0


def with_engine(fn, keep=False):
    eng = FlowEngine()
    try:
        eng.start()
    except SystemExit as e:
        return e.code or 1
    try:
        return fn(eng)
    finally:
        if keep:
            print("  (engine left running for next command)")
        else:
            try:
                eng.close()
            except Exception:
                pass


def cmd_generate(a):
    st = load_state()
    if a.project:
        st["project_id"] = a.project
        save_state(st)
    if a.model:
        st["model"] = a.model.upper()
        save_state(st)
    model = (a.model or st.get("model", "NARWHAL")).upper()
    pid = st.get("project_id", "")

    def _run(eng):
        if a.seed is not None:
            eng.set_gen_overrides(seed=int(a.seed))
        else:
            eng.set_gen_overrides(seed=None)
        if model != "NARWHAL" or st.get("model", "NARWHAL") != "NARWHAL":
            from flow_api import norm_model
            eng.set_gen_overrides(imageModelName=norm_model(model))
        else:
            eng.set_gen_overrides(imageModelName=None)
        aspect = a.aspect if a.aspect in ASPECTS else None
        if a.aspect and aspect is None:
            return fail(f"bad aspect (pick: {', '.join(ASPECTS)})", a.json, 1)
        if aspect is None:
            eng.set_gen_overrides(imageAspectRatio=None)
        items = eng.generate(a.prompt, model=model, aspect=aspect,
                             count=a.count, project_id=pid)
        if not items:
            return fail("generation failed", a.json)
        if a.upscale:
            ups = []
            for it in items[:1]:
                out = eng.upscale(it["uuid"], a.upscale.upper(), pid)
                ups.append(str(out) if out else "")
            if a.json:
                emit({"ok": True, "media": items, "upscaled": ups}, True)
            else:
                for it in items:
                    print(f"saved {it.get('file')} (seed={it.get('seed')})")
                print(f"upscaled: {ups[0] or 'FAILED'}")
            return 0 if ups[0] else 2
        if a.json:
            emit({"ok": True, "media": items}, True)
        else:
            for it in items:
                print(f"saved {it.get('file')} (seed={it.get('seed')} "
                      f"{it.get('width')}x{it.get('height')})")
        return 0

    return with_engine(_run, a.keep)


def cmd_upscale(a):
    st = load_state()
    pid = a.project or st.get("project_id", "")

    def _run(eng):
        out = eng.upscale(a.upscale_only, "2K", pid)
        if not out:
            return fail("upscale failed (see log; 4K is tier-gated)", a.json)
        emit({"ok": True, "file": str(out)} if a.json else f"saved {out}",
             a.json)
        return 0

    return with_engine(_run, a.keep)


def cmd_download(a):
    st = load_state()
    pid = a.project or st.get("project_id", "")

    def _run(eng):
        out = eng.download_media(a.download, pid)
        if not out:
            return fail("download failed", a.json)
        emit({"ok": True, "file": str(out)} if a.json else f"saved {out}",
             a.json)
        return 0

    return with_engine(_run, a.keep)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Google Flow one-shot image CLI")
    ap.add_argument("prompt", nargs="?", help="prompt to generate")
    ap.add_argument("-m", "--model", default="",
                    help="NARWHAL|HARBOR_SEAL|GEM_PIX_2 (aka Nano Banana 2|Lite|Pro)")
    ap.add_argument("-a", "--aspect", default="",
                    help="16:9|4:3|1:1|3:4|9:16")
    ap.add_argument("-n", "--count", type=int, default=1, help="images 1-4")
    ap.add_argument("-s", "--seed", type=int, default=None, help="fixed seed")
    ap.add_argument("--upscale", default="", help="also upscale first result: 2K")
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--credits", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--download", default="", metavar="UUID")
    ap.add_argument("--upscale-only", default="", metavar="UUID",
                    help="upscale existing media to 2K")
    ap.add_argument("--project", default="")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--reuse", default="",
                    help="regenerate prompt from history (uuid or 1-based index)")
    ap.add_argument("--keep", action="store_true",
                    help="leave engine running for the next command (faster)")
    a = ap.parse_args(argv)

    if a.project and not (a.prompt or a.history or a.credits or a.status
                          or a.download or a.upscale_only):
        st = load_state()
        st["project_id"] = a.project
        save_state(st)
        emit({"ok": True, "project": a.project} if a.json
             else f"project -> {a.project}", a.json)
        return 0
    if a.status:
        return cmd_status(a)
    if a.credits:
        return cmd_credits(a)
    if a.history:
        return cmd_history(a)
    if a.download:
        return cmd_download(a)
    if a.upscale_only:
        return cmd_upscale(a)
    if a.reuse:
        http = PureHTTP()
        items = http.history(load_state().get("project_id", "")) or []
        it = None
        if a.reuse.isdigit():
            try:
                it = items[int(a.reuse) - 1]
            except IndexError:
                pass
        else:
            it = next((x for x in items if x["uuid"].startswith(a.reuse)), None)
        if not it or not it.get("prompt"):
            return fail("reuse target not found", a.json)
        a.prompt = it["prompt"]
        if not a.json:
            print(f"  reusing prompt: {a.prompt[:70]!r}")
    if a.prompt:
        return cmd_generate(a)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
