"""Read GRIB2 messages with eccodes into cropped numpy arrays."""

from __future__ import annotations

from dataclasses import dataclass, field

import eccodes
import numpy as np


@dataclass
class Fields:
    lat: np.ndarray            # ascending
    lon: np.ndarray            # ascending, 0..360 convention
    data: dict = field(default_factory=dict)   # (shortName, level) -> 2D float32 [lat, lon]
    meta: dict = field(default_factory=dict)   # (shortName, level) -> metadata dict

    def __getitem__(self, key):
        return self.data[key]

    def crop(self, lat_min, lat_max, lon_min, lon_max):
        iy = np.where((self.lat >= lat_min - 1e-6) & (self.lat <= lat_max + 1e-6))[0]
        ix = np.where((self.lon >= lon_min - 1e-6) & (self.lon <= lon_max + 1e-6))[0]
        sub = Fields(self.lat[iy], self.lon[ix])
        for k, v in self.data.items():
            sub.data[k] = v[np.ix_(iy, ix)]
        sub.meta = dict(self.meta)
        return sub


META_KEYS = ["shortName", "units", "typeOfLevel", "level", "stepRange", "stepType",
             "dataDate", "dataTime", "name", "packingType", "bitsPerValue"]


def read(path: str, region: dict | None = None) -> Fields:
    """Read every message of a regular lat/lon GRIB file.

    The global grid (lon -180..179.75 or 0..359.75) is rolled to 0..360 so that
    domains crossing 180E work; then the read region is cropped.
    """
    out = None
    with open(path, "rb") as fh:
        while True:
            h = eccodes.codes_grib_new_from_file(fh)
            if h is None:
                break
            try:
                ni = eccodes.codes_get(h, "Ni")
                nj = eccodes.codes_get(h, "Nj")
                lat0 = eccodes.codes_get(h, "latitudeOfFirstGridPointInDegrees")
                lat1 = eccodes.codes_get(h, "latitudeOfLastGridPointInDegrees")
                lon0 = eccodes.codes_get(h, "longitudeOfFirstGridPointInDegrees")
                dx = eccodes.codes_get(h, "iDirectionIncrementInDegrees")
                vals = eccodes.codes_get_values(h).reshape(nj, ni).astype(np.float32)
                meta = {k: eccodes.codes_get(h, k) for k in META_KEYS}
            finally:
                eccodes.codes_release(h)
            lat = np.linspace(lat0, lat1, nj)
            lon = (lon0 + dx * np.arange(ni)) % 360.0
            order = np.argsort(lon)
            lon = lon[order]
            vals = vals[:, order]
            if lat[0] > lat[-1]:
                lat = lat[::-1]
                vals = vals[::-1]
            # append a wrap column so 360 (== 0) exists for domains ending at 360
            lon = np.append(lon, 360.0)
            vals = np.concatenate([vals, vals[:, :1]], axis=1)
            if out is None:
                out = Fields(lat, lon)
            if meta["shortName"] == "tp" and meta["units"] == "kg m**-2":
                vals = vals / 1000.0         # AIFS: kg m-2 (= mm) -> m, the IFS unit used downstream
                meta["units"] = "m"
            key = (meta["shortName"], int(meta["level"]))
            out.data[key] = vals
            out.meta[key] = meta
    if out is None:
        raise ValueError(f"no GRIB messages in {path}")
    if region:
        out = out.crop(region["lat_min"], region["lat_max"], region["lon_min"], region["lon_max"])
    return out
