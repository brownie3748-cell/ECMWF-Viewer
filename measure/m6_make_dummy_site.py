"""M6: build a dummy static site (~N MB of noise PNGs + an index.json) for a Pages deploy test."""

from __future__ import annotations

import json
import pathlib
import sys

import numpy as np
from PIL import Image


def main(out: str, size_mb: float):
    root = pathlib.Path(out)
    (root / "img").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    total, files = 0, []
    i = 0
    while total < size_mb * 1e6:
        # ~150 KB palette PNG, similar to a real chart
        a = rng.integers(0, 256, size=(600, 1000), dtype=np.uint8)
        a[:, ::2] = a[:, ::2] // 32 * 32
        p = root / "img" / f"dummy_{i:05d}.png"
        Image.fromarray(a, mode="L").quantize(64).save(p, optimize=True)
        total += p.stat().st_size
        files.append(p.name)
        i += 1
    (root / "index.json").write_text(json.dumps({"schema_version": 0, "dummy": True, "files": files[:50],
                                                 "count": len(files)}))
    (root / "index.html").write_text("<!doctype html><title>M6 Pages test</title><p>Stage 0 dummy site.</p>")
    print(f"{len(files)} files, {total / 1e6:.1f} MB")


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]))
