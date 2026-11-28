"""Object storage for the published site: Cloudflare R2 (S3 API) or a local directory (tests, offline).

Both backends count operations so that the monthly request budget can be enforced. Counts are an
upper-bound estimate: every write, list and delete call is counted as Class A, every read as Class B.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import logging
import mimetypes
import os
import pathlib
import shutil

log = logging.getLogger(__name__)

OPS_PREFIX = "_ops/"


class StorageCapError(RuntimeError):
    """Raised when an upload would exceed the storage cap or the monthly request budget."""


def _content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class _Base:
    def __init__(self, cfg: dict):
        self.scfg = cfg["storage"]
        self.ops = {"class_a": 0, "class_b": 0}
        self.month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
        self._month_ops = None

    # ------------------------------------------------------------ backend primitives
    def _get(self, key: str) -> bytes | None: raise NotImplementedError
    def _put(self, key: str, data: bytes, cache_control: str) -> None: raise NotImplementedError
    def _list(self) -> dict[str, int]: raise NotImplementedError
    def _delete(self, keys: list[str]) -> None: raise NotImplementedError

    # ------------------------------------------------------------ request budget
    def month_ops(self) -> dict:
        """Operations counted this calendar month (stored in _ops/YYYY-MM.json) plus this process."""
        if self._month_ops is None:
            raw = self._get(f"{OPS_PREFIX}{self.month}.json")
            self._month_ops = json.loads(raw) if raw else {"class_a": 0, "class_b": 0}
        return {k: self._month_ops.get(k, 0) + self.ops[k] for k in self.ops}

    def check_budget(self, planned_class_a: int = 0) -> None:
        used = self.month_ops()["class_a"]
        limit = self.scfg["max_class_a_per_month"]
        if used + planned_class_a > limit:
            raise StorageCapError(f"monthly Class A budget: {used} used + {planned_class_a} planned > {limit}")

    def save_ops(self) -> None:
        tot = self.month_ops()
        tot["class_a"] += 1                       # this write
        self._put(f"{OPS_PREFIX}{self.month}.json", json.dumps(tot).encode(), "no-cache")
        self._month_ops, self.ops = tot, {"class_a": 0, "class_b": 0}

    # ------------------------------------------------------------ public API
    def get_json(self, key: str):
        self.ops["class_b"] += 1
        raw = self._get(key)
        return json.loads(raw) if raw else None

    def get_bytes(self, key: str) -> bytes | None:
        self.ops["class_b"] += 1
        return self._get(key)

    def put_bytes(self, key: str, data: bytes, cache_control: str) -> None:
        self.ops["class_a"] += 1
        self._put(key, data, cache_control)

    def list_sizes(self) -> dict[str, int]:
        """{key: bytes} of every object in the bucket (one Class A call per 1000 keys)."""
        sizes = self._list()
        self.ops["class_a"] += max(1, -(-len(sizes) // 1000))
        return sizes

    def delete_keys(self, keys: list[str], sizes: dict[str, int]) -> int:
        """Delete these objects (1000 per call) and drop them from sizes."""
        keys = list(keys)
        for i in range(0, len(keys), 1000):
            self._delete(keys[i:i + 1000])
            self.ops["class_a"] += 1
        for k in keys:
            sizes.pop(k, None)
        return len(keys)

    def delete_prefix(self, prefix: str, sizes: dict[str, int]) -> int:
        keys = [k for k in sizes if k.startswith(prefix)]
        for i in range(0, len(keys), 1000):
            self._delete(keys[i:i + 1000])
            self.ops["class_a"] += 1
        for k in keys:
            sizes.pop(k, None)
        return len(keys)

    def upload_dir(self, local: pathlib.Path, prefix: str, sizes: dict[str, int], last: str | None = None,
                   keep_others: bool = False) -> int:
        """Upload every file under `local` to `prefix`, with `last` (e.g. meta.json) uploaded after the rest.

        keep_others: do not delete objects under `prefix` that are not in `local` (a resumed KIM run whose
        images were uploaded step by step).

        Enforces the storage cap and the request budget before the first byte is sent.
        Returns bytes uploaded and updates `sizes` in place.
        """
        files = sorted(p for p in local.rglob("*") if p.is_file())
        new_bytes = sum(p.stat().st_size for p in files)
        replaced = sum(v for k, v in sizes.items() if k.startswith(prefix))
        total_after = sum(sizes.values()) - replaced + new_bytes
        if total_after > self.scfg["max_bytes"]:
            raise StorageCapError(f"upload of {prefix} ({new_bytes / 1e6:.0f} MB) would bring the bucket to "
                                  f"{total_after / 1e9:.2f} GB > cap {self.scfg['max_bytes'] / 1e9:.2f} GB")
        self.check_budget(len(files))
        first = [p for p in files if p.name != last]
        tail = [p for p in files if p.name == last]

        def up(p: pathlib.Path):
            key = prefix + p.relative_to(local).as_posix()
            cc = self.scfg["image_cache_control"] if p.suffix in (".png", ".webp") else "no-cache, max-age=0"
            self._put(key, p.read_bytes(), cc)
            return key, p.stat().st_size

        uploaded = set()
        with cf.ThreadPoolExecutor(self.scfg["upload_threads"]) as ex:
            for key, n in ex.map(up, first):
                sizes[key] = n
                uploaded.add(key)
        for p in tail:
            key, n = up(p)
            sizes[key] = n
            uploaded.add(key)
        self.ops["class_a"] += len(files)
        stale = [] if keep_others else \
            [k for k in sizes if k.startswith(prefix) and k not in uploaded]       # e.g. a dropped chart
        for i in range(0, len(stale), 1000):
            self._delete(stale[i:i + 1000])
            self.ops["class_a"] += 1
        for k in stale:
            sizes.pop(k)
        return new_bytes


class LocalStore(_Base):
    """Directory-backed store with the same behaviour (used by tests and `--store local:<dir>`)."""

    def __init__(self, cfg: dict, root: str | os.PathLike):
        super().__init__(cfg)
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.name = f"local:{self.root}"

    def _get(self, key):
        p = self.root / key
        return p.read_bytes() if p.exists() else None

    def _put(self, key, data, cache_control):
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def _list(self):
        return {p.relative_to(self.root).as_posix(): p.stat().st_size
                for p in self.root.rglob("*") if p.is_file()}

    def _delete(self, keys):
        for k in keys:
            (self.root / k).unlink(missing_ok=True)
        for d in sorted((p for p in self.root.rglob("*") if p.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()


class R2Store(_Base):
    """Cloudflare R2 through its S3-compatible API. Credentials come from the environment."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        import boto3
        from botocore.config import Config

        missing = [v for v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY") if not os.environ.get(v)]
        if missing:
            raise RuntimeError(f"missing environment variables: {missing}")
        self.bucket = os.environ.get("R2_BUCKET") or self.scfg["bucket"]
        self.name = f"r2:{self.bucket}"
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=Config(retries={"max_attempts": 5, "mode": "standard"},
                          max_pool_connections=max(10, self.scfg["upload_threads"]),
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )

    def _get(self, key):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def _put(self, key, data, cache_control):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=_content_type(key),
                           CacheControl=cache_control)

    def _list(self):
        out = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
            for o in page.get("Contents", []):
                out[o["Key"]] = o["Size"]
        return out

    def _delete(self, keys):
        self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True})


def open_store(cfg: dict, spec: str):
    """`r2` or `local:<dir>`."""
    if spec == "r2":
        return R2Store(cfg)
    if spec.startswith("local:"):
        return LocalStore(cfg, spec[len("local:"):])
    raise ValueError(f"unknown store {spec!r}")


def clear_dir(p: pathlib.Path) -> None:
    shutil.rmtree(p, ignore_errors=True)
