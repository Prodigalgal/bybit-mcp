"""Write only this service's GitOps directory with an immutable image digest."""
import argparse
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "registry.example.invalid/bybit-mcp"


def promote(gitops, digest, commit):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("A full SHA-256 image digest and 40-character source commit are required")
    source = ROOT / "deploy/gitops"
    resources = list(yaml.safe_load_all((source / "runtime.yaml").read_text(encoding="utf-8")))
    image = os.environ.get("IMAGE_REPOSITORY", IMAGE)
    node = os.environ.get("DEPLOY_NODE_NAME", "bybit-worker")
    gateway = os.environ.get("GATEWAY_NAME", "application-gateway")
    url = urlparse(os.environ.get("PUBLIC_URL", "https://bybit-mcp.example.invalid"))
    if not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9/_.-]+", image) or not re.fullmatch(r"[A-Za-z0-9._-]+", node) or not re.fullmatch(r"[a-z0-9.-]+", gateway):
        raise ValueError("Invalid deployment image repository or node name")
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
        raise ValueError("PUBLIC_URL must be an HTTPS origin")
    allowed = {"Namespace", "StorageClass", "PersistentVolume", "PersistentVolumeClaim", "Deployment", "Service", "HTTPRoute"}
    if any(r["kind"] not in allowed for r in resources):
        raise ValueError("Runtime GitOps must not include Secrets or unrelated resource kinds")
    deployment = next(r for r in resources if r["kind"] == "Deployment")
    spec = deployment["spec"]
    if spec["replicas"] != 1 or spec["strategy"]["type"] != "Recreate" or spec["template"]["spec"]["nodeSelector"] != {"kubernetes.io/hostname": "bybit-worker"}:
        raise ValueError("SQLite deployment must remain single-replica Recreate on its configured worker")
    spec["template"]["spec"]["nodeSelector"]["kubernetes.io/hostname"] = node
    for resource in resources:
        if resource["kind"] in {"Namespace", "StorageClass", "PersistentVolume", "PersistentVolumeClaim"}:
            if resource["metadata"]["annotations"].get("argocd.argoproj.io/sync-options") != "Prune=false,Delete=false":
                raise ValueError("Persistent data resources must be protected from Argo pruning/deletion")
    container = spec["template"]["spec"]["containers"][0]
    if not container["image"].startswith(IMAGE + ":"):
        raise ValueError("Unexpected runtime image repository")
    container["image"] = image + ":gitops"
    for resource in resources:
        if resource["kind"] == "PersistentVolume":
            for term in resource["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"]:
                for match in term["matchExpressions"]:
                    if match["key"] == "kubernetes.io/hostname":
                        match["values"] = [node]
        if resource["kind"] == "HTTPRoute":
            resource["spec"]["hostnames"] = [url.hostname]
            for parent in resource["spec"]["parentRefs"]:
                parent["name"] = gateway
    container["env"] = [e for e in container.get("env", []) if e["name"] != "IMAGE_DIGEST"] + [{"name": "IMAGE_DIGEST", "value": digest}]
    destination = Path(gitops).resolve() / "bybit-mcp"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "runtime.yaml").write_text(yaml.safe_dump_all(resources, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (destination / "kustomization.yaml").write_text(yaml.safe_dump({"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": ["runtime.yaml"],
        "images": [{"name": image, "newName": image, "digest": digest}]}, sort_keys=False), encoding="utf-8")
    (destination / "release.json").write_text(json.dumps({"source_repository": "https://github.com/Prodigalgal/bybit-mcp", "source_commit": commit, "image": image + "@" + digest}, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(source / "README.md", destination / "README.md")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gitops", type=Path, required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    promote(args.gitops, args.digest, args.commit)
