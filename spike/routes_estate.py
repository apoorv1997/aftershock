#!/usr/bin/env python3
"""Route contract at estate level: routes one service exposed at an old ref that no service of the estate
exposes at HEAD (a route that moved to another service or under a mount prefix still answers), against
URL literals in a consumer. Usage: routes_estate.py --lib <path> --old <ref> --estate <path>=<ref> ... --consumer <dir>"""
from __future__ import annotations

import argparse
from cie import routes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", required=True); ap.add_argument("--old", required=True); ap.add_argument("--new", default="HEAD")
    ap.add_argument("--estate", action="append", default=[], help="<repo path>=<ref> whose HEAD routes count as surviving")
    ap.add_argument("--consumer"); ap.add_argument("--ext", default=".ts,.tsx,.js,.jsx")
    a = ap.parse_args()
    old = routes.routes(a.lib, a.old); new = routes.routes(a.lib, a.new)
    surviving = routes.estate_routes([tuple(e.split("=", 1)) for e in a.estate])   # mounts resolved across repositories
    d = routes.diff(old, new, surviving)
    print(f"{a.lib}: {len(old)} routes at {a.old[:10]} -> {len(new)} at {a.new}; estate surviving routes {len(surviving)}; gone from the estate: {len(d['removed'])}")
    for r in d["removed"]:
        print(f"  gone  {r.method.upper():8s} {r.path}   ({r.file}:{r.line})")
    if a.consumer:
        hits = routes.consumers(a.consumer, [r.path for r in d["removed"]], tuple(a.ext.split(",")))
        n = sum(len(h) for h in hits.values())
        print(f"\nconsumer {a.consumer}: {n} lines name a route gone from the estate")
        for r in d["removed"]:
            for f, ln, code in hits.get(r.path, []):
                print(f"  {f}:{ln}  {r.method.upper()} {r.path}  |  {code[:110]}")


if __name__ == "__main__":
    main()
