"""Record that the plotting stack imports and report library versions."""

from __future__ import annotations

import importlib.metadata as md

from measure.common import md_table, save

PKGS = ["ecmwf-opendata", "eccodes", "eccodeslib", "numpy", "scipy", "matplotlib", "Cartopy", "pillow",
        "shapely", "pyproj"]


def main():
    rows, ok = [], True
    for p in PKGS:
        try:
            rows.append([p, md.version(p)])
        except md.PackageNotFoundError:
            rows.append([p, "MISSING"])
            ok = False
    import cartopy.feature  # noqa: F401
    import eccodes
    from PIL import features
    rows.append(["ecCodes library", eccodes.codes_get_api_version()])
    rows.append(["Pillow WebP support", features.check("webp")])
    status = "PASS" if ok and features.check("webp") else "FAIL"
    save("install", {"status": status, "versions": rows},
         f"## Install check — **{status}**\n\n" + md_table(["package", "version"], rows))


if __name__ == "__main__":
    main()
