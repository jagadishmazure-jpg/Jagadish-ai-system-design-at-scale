"""In-cluster load loop: hit the Service for N seconds, count failures and versions seen."""
import json
import sys
import time
import urllib.request

url, seconds = sys.argv[1], float(sys.argv[2])
ok = fail = 0
versions: dict[str, int] = {}
end = time.time() + seconds
while time.time() < end:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            v = json.loads(r.read())["version"]
            versions[v] = versions.get(v, 0) + 1
            ok += 1
    except Exception:  # noqa: BLE001 - any failure counts
        fail += 1
    time.sleep(0.02)
print("RESULT " + json.dumps({"ok": ok, "failed": fail, "versions": versions}))
