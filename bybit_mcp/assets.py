"""Content-addressed UI assets keep HTML and scripts consistent across CDN caches."""
import hashlib
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response


def install_assets(app, templates, directory: Path):
    assets = {}
    for path in directory.iterdir():
        if path.suffix not in {".js", ".css"} or not path.is_file():
            continue
        payload = path.read_bytes()
        revision = hashlib.sha256(payload).hexdigest()[:16]
        assets[path.name] = {"revision": revision, "payload": payload,
                             "media_type": "text/javascript" if path.suffix == ".js" else "text/css"}

    def asset_url(name):
        return f'/assets/{assets[name]["revision"]}/{name}'

    templates.env.globals["asset_url"] = asset_url

    @app.get("/assets/{revision}/{name}")
    async def asset(revision: str, name: str):
        item = assets.get(name)
        if not item or revision != item["revision"]:
            raise HTTPException(404, "Asset revision not found")
        return Response(item["payload"], media_type=item["media_type"],
                        headers={"Cache-Control": "public, max-age=31536000, immutable",
                                 "ETag": '"' + item["revision"] + '"'})
