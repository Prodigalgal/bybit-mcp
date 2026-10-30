"""Public, read-only rollout acceptance; never receives a Bybit or admin key."""
import argparse
import json
import os
import time
import urllib.error
import urllib.request


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Cache-Control": "no-cache"}), timeout=12) as response:
        return json.load(response)


def verify(url, commit, digest, timeout=720):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            health = get(url + "/healthz?release=" + digest[7:])
            if health.get("status") == "ok" and health.get("revision") == commit and health.get("image_digest") == digest:
                assert get(url + "/readyz")["status"] == "ready"
                assert get(url + "/.well-known/oauth-protected-resource/mcp")["resource"] == url + "/mcp"
                request = urllib.request.Request(url + "/mcp", data=b"{}", headers={"Content-Type": "application/json"})
                try:
                    urllib.request.urlopen(request, timeout=12)
                    raise AssertionError("MCP accepted an unauthenticated request")
                except urllib.error.HTTPError as error:
                    assert error.code == 401 and "resource_metadata=" in error.headers.get("WWW-Authenticate", "")
                print(json.dumps({"status": "verified", "revision": commit, "image_digest": digest, "public_oauth_metadata": True, "unauthenticated_mcp": 401}))
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(8)
    raise SystemExit("Rollout did not expose the promoted source revision and image digest within the acceptance window")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("PUBLIC_URL"), required=not os.environ.get("PUBLIC_URL"))
    parser.add_argument("--commit", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--timeout", type=int, default=720)
    args = parser.parse_args()
    verify(args.url.rstrip("/"), args.commit, args.digest, args.timeout)
