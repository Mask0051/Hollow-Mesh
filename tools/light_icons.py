"""Make light copies of the black icons in docs/icons/: light fill, dark outline, so
they read on dark backgrounds (GitHub dark mode) and light ones. Re-run after changing a design:

    python tools/light_icons.py

Each icon is one embedded image placed by a <use>; we add a colour filter to it.
"""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "docs" / "icons"
OUT = SRC / "light"
OUTLINE = 12  # outline thickness, in the icon's 512-unit space (~0.5 px at 22 px wide)
# Light fill with a dark outline, so the icon reads on dark AND light backgrounds:
# grow the shape (dilate), paint that dark, then lay the light-coloured shape on top.
FILTER = (f'<filter id="light" x="-15%" y="-15%" width="130%" height="130%">'
          f'<feMorphology in="SourceAlpha" operator="dilate" radius="{OUTLINE}" result="grow"/>'
          '<feFlood flood-color="#1a1a19"/><feComposite in2="grow" operator="in" result="outline"/>'
          '<feColorMatrix in="SourceGraphic" type="matrix" '
          'values="0 0 0 0 0.94  0 0 0 0 0.937  0 0 0 0 0.925  0 0 0 1 0" result="fill"/>'
          '<feMerge><feMergeNode in="outline"/><feMergeNode in="fill"/></feMerge></filter>')

OUT.mkdir(exist_ok=True)
for f in sorted(SRC.glob("*.svg")):
    svg = f.read_text()
    svg, n1 = re.subn(r"<defs>", "<defs>" + FILTER, svg, count=1)
    svg, n2 = re.subn(r"<use ", '<use filter="url(#light)" ', svg)
    if n1 != 1 or n2 < 1:
        raise SystemExit(f"{f.name}: unexpected structure, not converted")
    (OUT / f.name).write_text(svg)
    print("wrote", OUT / f.name)
