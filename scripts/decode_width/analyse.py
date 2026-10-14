"""Score read_frames.py output per frame, per plate-px band and per passage.

analyse.py name=PLATE[,name=PLATE...] [BAR]   (BAR: exact-match bar, 0.75 = standard by day)
"""
import json, sys, statistics as st
from pathlib import Path
import os
OUT = Path(os.environ.get("DW_ROOT", ".")).resolve() / "out"
VARS = ["1920-sweep", "1920-cap1920", "2560-sweep", "2560-cap1920", "2560-cap2560",
        "3200-sweep", "3200-cap1920", "3200-cap2560", "3200-cap3200"]
BANDS = [(0, 150), (150, 200), (200, 250), (250, 300), (300, 10**5)]
BAR = float(sys.argv[2]) if len(sys.argv) > 2 else 0.75
def lev(a, b):
    p = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        c = [i]
        for j, y in enumerate(b, 1):
            c.append(min(p[j] + 1, c[j - 1] + 1, p[j - 1] + (x != y)))
        p = c
    return p[-1]
passages = [tuple(a.split("=")) for a in sys.argv[1].split(",")]  # name=PLATE
agg = {v: {b: [0, 0] for b in BANDS} for v in VARS}
ms = {v: [] for v in VARS}
firsts = {}
for name, truth in passages:
    rows = {v: {json.loads(l)["i"]: json.loads(l) for l in open(OUT / name / f"{v}.jsonl")} for v in VARS}
    idx = sorted(rows[VARS[0]])
    # frames of THIS car: some variant read text within 2 edits of the truth
    car = [i for i in idx if any(rows[v][i]["plate"] and lev(rows[v][i]["plate"], truth) <= 2 for v in VARS)]
    if not car:
        print(name, "no frames of the car"); continue
    # The approach: walk back from the first frame read as this car while some
    # variant still boxes a plate (gaps of up to 3 s), at most 20 s. t0, the
    # car's first frame, is the first of those; every "first" is measured from it.
    boxed = lambda i: any(rows[v][i]["px"] for v in VARS)
    start = car[0]; tc = rows[VARS[0]][car[0]]["t"]; last = tc
    for i in reversed([i for i in idx if i < car[0]]):
        t = rows[VARS[0]][i]["t"]
        if tc - t > 20 or last - t > 3: break
        if boxed(i): start, last = i, t
    t0 = rows[VARS[0]][start]["t"]
    span = [i for i in idx if start <= i <= car[-1]]
    # A frame's plate width: the median of every variant's box (4K-equivalent px).
    ref = {}
    for i in span:
        pxs = [rows[v][i]["px"] for v in VARS if rows[v][i]["px"]]
        ref[i] = st.median(pxs) if pxs else None
    print(f"\n== {name} {truth}: {len(span)} frames {span[0]}-{span[-1]}, car-read frames {len(car)}, "
          f"t0={t0:.2f}, span {rows[VARS[0]][span[-1]]['t']-t0:.1f}s, px range {min(p for p in ref.values() if p)}-{max(p for p in ref.values() if p)}")
    line = []
    for v in VARS:
        ok = {i: rows[v][i]["plate"] == truth and rows[v][i]["score"] >= BAR for i in span}
        for i in span:
            if ref[i] is None: continue
            for b in BANDS:
                if b[0] <= ref[i] < b[1]:
                    agg[v][b][1] += 1; agg[v][b][0] += ok[i]
        hit = next((i for i in span if ok[i] and ref[i] is not None), None)
        first = None if hit is None else rows[v][hit]["t"] - t0
        n_ok = sum(ok[i] for i in span if ref[i] is not None); n = sum(1 for i in span if ref[i] is not None)
        firsts.setdefault(v, {})[name] = first
        ms[v] += [rows[v][i]["read_ms"] for i in span]
        wrong = sum(1 for i in span if rows[v][i]["plate"] and rows[v][i]["plate"] != truth and rows[v][i]["score"] >= BAR)
        at = "" if hit is None else f" at {ref[hit]:.0f} px"
        line.append(f"  {v:13s} correct {n_ok:3d}/{n:3d}  first {first if first is None else round(first,2)} s{at}  wrong>=bar {wrong}")
    print("\n".join(line))
print(f"\n== by plate px band (bar {BAR}) ==")
print(f"{'variant':13s} " + " ".join(f"{a}-{b if b<10**5 else ''}".rjust(12) for a, b in BANDS) + "   all")
for v in VARS:
    cells = [f"{agg[v][b][0]}/{agg[v][b][1]}".rjust(12) for b in BANDS]
    tot = [sum(agg[v][b][0] for b in BANDS), sum(agg[v][b][1] for b in BANDS)]
    print(f"{v:13s} " + " ".join(cells) + f"   {tot[0]}/{tot[1]} ({100*tot[0]/max(1,tot[1]):.0f}%)")
print("\n== median read ms (Mac, nine runs in parallel: not a timing) ==")
for v in VARS: print(v, round(st.median(ms[v]), 1) if ms[v] else None)
