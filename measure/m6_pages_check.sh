#!/usr/bin/env bash
# M6: inspect GitHub Pages response headers (CORS, caching, content type) for JSON and PNG.
# Usage: measure/m6_pages_check.sh https://<owner>.github.io/<repo>
set -euo pipefail
base="${1:?base URL}"
for path in index.json img/dummy_00000.png index.html; do
  echo "=== $path"
  curl -sSI -H "Origin: https://example.org" "$base/$path" \
    | grep -iE "^(HTTP|content-type|content-length|cache-control|access-control-|etag|last-modified|age|x-cache|expires)"
  curl -s -o /dev/null -w "time_total=%{time_total}s size=%{size_download}\n" "$base/$path"
done
