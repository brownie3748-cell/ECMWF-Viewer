"""Concatenate M1-M5 markdown into one report (printed; the workflow appends it to the job summary)."""

from __future__ import annotations

import platform
import sys

from measure.common import RESULTS, load

ORDER = ["install", "m1", "m2", "m3", "m4", "m5", "m7", "m8", "m9", "m10"]


def main():
    out = ["# Stage 0 measurement report\n",
           f"Python {sys.version.split()[0]} on {platform.platform()}\n",
           "| item | status |", "|---|---|"]
    for m in ORDER:
        d = load(m)
        out.append(f"| {m} | {d.get('status', '?') if d else 'not run'} |")
    out.append("")
    for m in ORDER:
        p = RESULTS / f"{m}.md"
        if p.exists():
            out.append(p.read_text())
    print("\n".join(out))


if __name__ == "__main__":
    main()
