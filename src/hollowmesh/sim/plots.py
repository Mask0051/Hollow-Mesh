"""README charts from results.json (`make metrics` runs this after the metrics).

Plain SVG, no plotting library: one light and one dark file per chart, so the README can
pick by the reader's theme with <picture>. The numbers are always the ones in results.json.

    python -m hollowmesh.sim.plots [results.json] [docs/img]
"""

from __future__ import annotations

import json
import pathlib
import sys
from html import escape
from typing import Any

FONT = "system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
THEMES = {  # validated pair: blue = Countertally / first measure, orange = Directorate / second
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
              "grid": "#e1e0d9", "axis": "#c3c2b7", "s1": "#2a78d6", "s2": "#eb6834"},
    "dark": {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
             "grid": "#2c2c2a", "axis": "#383835", "s1": "#3987e5", "s2": "#d95926"},
}
W = 720


def _nice(v: float) -> float:
    """A round axis maximum just above v."""
    for step in (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if v <= step * 5:
            return step * ((int(v) // step) + 1)
    return v * 1.1


def _t(x: float, y: float, s: str, c: str, size: int = 12, anchor: str = "start",
       weight: int = 400) -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" fill="{c}" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}">{escape(s)}</text>')


def _frame(h: int, th: dict[str, str], title: str, sub: str, body: list[str]) -> str:
    return "\n".join([
        (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" viewBox="0 0 {W} {h}" '
         f'font-family="{FONT}" role="img" aria-label="{escape(title)}">'),
        f'<title>{escape(title)}</title>',
        f'<rect width="{W}" height="{h}" rx="10" fill="{th["surface"]}"/>',
        _t(24, 34, title, th["ink"], 16, weight=600),
        _t(24, 54, sub, th["ink2"], 12),
        *body, "</svg>", ""])


def _legend(x: float, y: float, items: list[tuple[str, str, bool]], th: dict[str, str],
            box: bool = False) -> list[str]:
    out = []
    for label, color, dashed in items:
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        if box:  # bars: a filled square
            out.append(f'<rect x="{x + 4}" y="{y - 10}" width="11" height="11" rx="2" fill="{color}"/>')
        else:
            out.append(f'<line x1="{x}" y1="{y - 4}" x2="{x + 18}" y2="{y - 4}" stroke="{color}" '
                       f'stroke-width="2.5" stroke-linecap="round"{dash}/>')
        out.append(_t(x + 24, y, label, th["ink2"], 12))
        x += 34 + 7 * len(label)
    return out


def outbreak(r: dict[str, Any], th: dict[str, str]) -> str:
    c = r["countertally"]["network_cut"]
    tr = c["trace"]
    hours = [0, *tr["hour"]]
    series = [  # (name, values, color, dashed)
        ("Real cases", [0, *tr["truth"]], th["ink2"], True),
        ("Countertally", [0, *tr["countertally"]], th["s1"], False),
        ("Directorate", [0, *tr["official"]], th["s2"], False),
    ]
    h, left, right, top, bottom = 360, 56, W - 150, 96, 360 - 44
    ymax = _nice(max(max(v) for _, v, _, _ in series))
    xmax = hours[-1]
    X = lambda x: left + (right - left) * x / xmax
    Y = lambda y: bottom - (bottom - top) * y / ymax
    body = _legend(24, 80, [(n, col, d) for n, _, col, d in series], th)
    n = 5 if ymax % 5 == 0 and (ymax / 5) % 1 == 0 else 4  # whole-number gridlines
    for i in range(n + 1):  # gridlines + y labels
        v = ymax * i / n
        body.append(f'<line x1="{left}" y1="{Y(v):.1f}" x2="{right}" y2="{Y(v):.1f}" '
                    f'stroke="{th["axis"] if i == 0 else th["grid"]}" stroke-width="1"/>')
        body.append(_t(left - 8, Y(v) + 4, f"{v:g}", th["muted"], 11, "end"))
    for hr in range(0, xmax + 1, 12):
        body.append(_t(X(hr), bottom + 18, f"{hr} h", th["muted"], 11, "middle"))
    body.append(_t((left + right) / 2, bottom + 36, "sim time since the network was cut",
                   th["muted"], 11, "middle"))
    ends = []
    for name, vals, col, dashed in series:
        pts = " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in zip(hours, vals, strict=True))
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        body.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2" '
                    f'stroke-linejoin="round" stroke-linecap="round"{dash}/>')
        body.append(f'<circle cx="{X(hours[-1]):.1f}" cy="{Y(vals[-1]):.1f}" r="4" fill="{col}" '
                    f'stroke="{th["surface"]}" stroke-width="2"/>')
        ends.append([Y(vals[-1]), f"{vals[-1]} {name}"])
    ends.sort()
    for i in range(1, len(ends)):  # keep end labels 15px apart
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 15)
    body += [_t(right + 10, y + 4, s, th["ink"], 12) for y, s in ends]
    return _frame(h, th, "Lung sickness: what each side can see",
                  f"Network cut for 2 sim days · total cases, all levels · one run (seed "
                  f"{c['trace_seed']}) of the {len(r['about']['seeds'])} in results.json", body)


ROWS = [("wifi_on", "Wi-Fi on"), ("mesh_only", "Wi-Fi cut: mesh only"),
        ("mesh_only_25pct_phones_dead", "… 25% of phones dead"),
        ("mesh_only_50pct_phones_dead", "… 50% of phones dead"),
        ("mesh_only_75pct_phones_dead", "… 75% of phones dead"),
        ("mesh_only_deep_blackout", "… Deep blacked out")]


def _bar(x: float, y: float, w: float, hh: float, col: str) -> str:
    """Bar anchored at the left baseline, 4px rounded data end on the right."""
    rr = min(4.0, w / 2, hh / 2)
    if w <= 0:
        return ""
    return (f'<path d="M{x:.1f},{y:.1f} h{w - rr:.1f} a{rr},{rr} 0 0 1 {rr},{rr} '
            f'v{hh - 2 * rr:.1f} a{rr},{rr} 0 0 1 {-rr},{rr} h{-(w - rr):.1f} z" fill="{col}"/>')


def delivery(r: dict[str, Any], th: dict[str, str]) -> str:
    d = r["delivery"]
    meas = [("Reached all 4 levels", "minutes_to_all_4_levels", th["s1"]),
            ("Reached 95% of phones", "minutes_to_95pct", th["s2"])]
    left, right, top, bh, gap, row = 190, W - 70, 100, 11, 2, 40
    h = top + row * len(ROWS) + 40
    xmax = _nice(max(d[k][m].get("median", 0) for k, _ in ROWS for _, m, _ in meas))
    X = lambda v: left + (right - left) * v / xmax
    body = _legend(24, 80, [(n, col, False) for n, _, col in meas], th, box=True)
    step = 30 if xmax <= 180 else 60
    for v in range(0, int(xmax) + 1, step):
        body.append(f'<line x1="{X(v):.1f}" y1="{top - 6}" x2="{X(v):.1f}" y2="{h - 34}" '
                    f'stroke="{th["axis"] if v == 0 else th["grid"]}" stroke-width="1"/>')
        body.append(_t(X(v), h - 18, f"{v} min", th["muted"], 11, "middle"))
    for i, (key, label) in enumerate(ROWS):
        y0 = top + i * row
        body.append(_t(left - 12, y0 + bh + 5, label, th["ink"], 12, "end"))
        for j, (_, m, col) in enumerate(meas):
            v = d[key][m].get("median")
            y = y0 + j * (bh + gap)
            if v is None:
                body.append(_t(left + 6, y + bh - 1, "never", th["muted"], 11))
                continue
            body.append(_bar(left, y, X(v) - left, bh, col))
            body.append(_t(X(v) + 6, y + bh - 1, f"{v:g}", th["ink2"], 11))
    pct = min(d[k]["delivered_share_within_1_day"]["min"] for k, _ in ROWS)
    return _frame(h, th, "An SOS from the deepest level, as the network fails",
                  f"Median sim-minutes over {len(r['about']['seeds'])} runs, "
                  f"{r['about']['residents']} residents · every run delivered to "
                  f"{pct * 100:.0f}% of working phones within a day", body)


CHARTS = {"outbreak": outbreak, "delivery": delivery}


def main(argv: list[str] | None = None) -> None:
    a = sys.argv[1:] if argv is None else argv
    src = pathlib.Path(a[0] if a else "results.json")
    out = pathlib.Path(a[1] if len(a) > 1 else "docs/img")
    out.mkdir(parents=True, exist_ok=True)
    r = json.loads(src.read_text())
    for name, fn in CHARTS.items():
        for mode, th in THEMES.items():
            (out / f"{name}-{mode}.svg").write_text(fn(r, th))
    print(f"wrote {len(CHARTS) * len(THEMES)} charts to {out}/")


if __name__ == "__main__":
    main()
