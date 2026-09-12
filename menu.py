#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flow guided menu — write prompts, pick features by number. No typing UUIDs.

Run:  python menu.py        (or: flow.bat, then it is the default screen)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from flow_api import FlowEngine, PureHTTP, load_state, save_state, ASPECTS  # noqa: E402

MODELS = ["NARWHAL", "HARBOR_SEAL", "GEM_PIX_2"]
MODEL_LABELS = {"NARWHAL": "Nano Banana 2", "HARBOR_SEAL": "Nano Banana 2 Lite",
                "GEM_PIX_2": "Nano Banana Pro"}


def clear():
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass


class App:
    def __init__(self):
        self.http = PureHTTP()
        self.eng = None
        st = load_state()
        self.model = st.get("model", "NARWHAL")
        self.seed = None
        self.aspect = None
        self.count = 1
        self.pid = st.get("project_id", "")
        self._items = []

    # ── helpers ──────────────────────────────────────────────
    @staticmethod
    def ask(text, default=""):
        try:
            s = input(f"{text} [{default}]: " if default else f"{text}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return s if s else default

    def engine(self):
        if self.eng is None:
            print("  booting engine (one time)...")
            self.eng = FlowEngine()
            try:
                self.eng.start()
            except SystemExit:
                self.eng = None
                return None
            if self.model != "NARWHAL":
                self.eng.set_gen_overrides(imageModelName=self.model)
        return self.eng

    def refresh(self):
        items = self.http.history(self.pid)
        self._items = items or []
        return self._items

    def pick_media(self):
        items = self.refresh()
        if not items:
            print("  [!] history empty/failed")
            return None
        for i, d in enumerate(items[:15], 1):
            print(f"  {i:2d}. {str(d['prompt'])[:50]!r} "
                  f"{d['width']}x{d['height']} seed={d['seed']}")
        s = self.ask("pick number", "1")
        if s is None:
            return None
        try:
            return items[int(s) - 1]
        except (ValueError, IndexError):
            print("  [!] bad number")
            return None

    # ── features ─────────────────────────────────────────────
    def f_generate(self):
        prompt = self.ask("prompt")
        if prompt is None:
            return
        if not prompt:
            print("  [!] empty prompt")
            return
        m = self.ask(f"model ({'/'.join(MODELS)})", self.model).upper()
        if m not in MODELS:
            m = self.model
        self.model = m
        st = load_state()
        st["model"] = m
        save_state(st)
        a = self.ask(f"aspect ({'/'.join(ASPECTS)}; blank=default)", "")
        if a is None:
            return
        self.aspect = a if a in ASPECTS else None
        n = self.ask("count 1-4", str(self.count))
        if n is None:
            return
        self.count = max(1, min(4, int(n) if n.isdigit() else 1))
        s = self.ask("seed (blank = random)", "" if self.seed is None else str(self.seed))
        if s is None:
            return
        self.seed = int(s) if s.isdigit() else None
        eng = self.engine()
        if not eng:
            return
        eng.set_gen_overrides(imageModelName=m)
        eng.set_gen_overrides(imageAspectRatio=self.aspect)
        eng.set_gen_overrides(seed=self.seed)
        print(f"  generating {self.count}x with {m} {self.aspect or ''}...")
        items = eng.generate(prompt, model=m, aspect=self.aspect,
                             count=self.count, project_id=self.pid)
        if not items:
            print("  [!] generation failed (see flow.log)")
            return
        for it in items:
            print(f"  saved {it.get('file')} (seed={it.get('seed')})")
        if self.ask("upscale first result to 2K? (y/n)", "n").lower() == "y":
            out = eng.upscale(items[0]["uuid"], "2K", self.pid)
            print(f"  upscaled: {out or 'FAILED'}")

    def f_upscale(self):
        it = self.pick_media()
        if not it:
            return
        eng = self.engine()
        if not eng:
            return
        out = eng.upscale(it["uuid"], "2K", self.pid)
        print(f"  upscaled: {out or 'FAILED (4K is tier-gated; 2K only)'}")

    def f_history(self):
        items = self.refresh()
        if items is None:
            print("  [!] history failed (sign in once, see README §5)")
            return
        print(f"  {len(items)} items:")
        for i, d in enumerate(items[:15], 1):
            print(f"  {i:2d}. {str(d['prompt'])[:50]!r} seed={d['seed']} "
                  f"{d['width']}x{d['height']} {d['size']}B")

    def f_download(self):
        it = self.pick_media()
        if not it:
            return
        eng = self.engine()
        if not eng:
            return
        out = eng.download_media(it["uuid"], self.pid)
        print(f"  saved {out}" if out else "  [!] download failed")

    def f_credits(self):
        c = self.http.credits()
        print(f"  credits: {c.get('credits')}" if c else "  [!] credits failed")

    def f_settings(self):
        print(f"  model={self.model} seed={self.seed or 'random'} project={self.pid}")
        m = self.ask(f"model ({'/'.join(MODELS)})", self.model).upper()
        if m in MODELS:
            self.model = m
            st = load_state()
            st["model"] = m
            save_state(st)
            if self.eng:
                self.eng.set_gen_overrides(imageModelName=m)
        s = self.ask("seed (blank = random)", "")
        if s is None:
            return
        self.seed = int(s) if s.isdigit() else None
        p = self.ask("project id (blank = keep)", "")
        if p is None:
            return
        if p:
            self.pid = p
            st = load_state()
            st["project_id"] = p
            save_state(st)
            self.http.reset()

    # ── loop ─────────────────────────────────────────────────
    def header(self):
        c = self.http.credits()
        cr = c.get("credits") if c else "?"
        ml = MODEL_LABELS.get(self.model, self.model)
        print("+" + "-" * 56 + "+")
        print(f"| Google Flow   {ml:<18} {self.aspect or 'default':<7} "
              f"x{self.count} credits={cr:<4}|")
        print("+" + "-" * 56 + "+")

    def run(self):
        menu = [("Generate image  - write a prompt, pick options", self.f_generate),
                ("Upscale to 2K   - pick from your images", self.f_upscale),
                ("History         - latest images", self.f_history),
                ("Download        - save an image again", self.f_download),
                ("Credits         - remaining balance", self.f_credits),
                ("Settings        - model / seed / project", self.f_settings)]
        while True:
            clear()
            self.header()
            for i, (name, _) in enumerate(menu, 1):
                print(f"  {i}. {name}")
            print("  0. Quit")
            print("-" * 58)
            try:
                s = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                s = "0"
            if s in ("0", "q", "quit", "exit"):
                break
            try:
                menu[int(s) - 1][1]()
            except (ValueError, IndexError):
                print("  pick 0-%d" % len(menu))
            except KeyboardInterrupt:
                print("\n  cancelled")
            try:
                input("  [enter] back... ")
            except (EOFError, KeyboardInterrupt):
                pass
        if self.eng:
            try:
                self.eng.close()
            except Exception:
                pass
        print("bye!")


if __name__ == "__main__":
    App().run()
