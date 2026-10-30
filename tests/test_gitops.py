import json

import pytest
import yaml

from scripts.promote_gitops import promote, IMAGE


def test_gitops_promotion_is_immutable_preserves_storage_and_excludes_secrets(tmp_path):
    unrelated = tmp_path / "existing-service.yaml"
    unrelated.write_text("preserve\n")
    digest, commit = "sha256:" + "a" * 64, "b" * 40
    promote(tmp_path, digest, commit)
    resources = list(yaml.safe_load_all((tmp_path / "bybit-mcp/runtime.yaml").read_text()))
    assert all(r["kind"] != "Secret" for r in resources)
    protected = [r for r in resources if r["kind"] in {"Namespace", "StorageClass", "PersistentVolume", "PersistentVolumeClaim"}]
    assert len(protected) == 4
    assert all(r["metadata"]["annotations"]["argocd.argoproj.io/sync-options"] == "Prune=false,Delete=false" for r in protected)
    deployment = next(r for r in resources if r["kind"] == "Deployment")
    assert deployment["spec"]["template"]["spec"]["containers"][0]["env"] == [{"name": "IMAGE_DIGEST", "value": digest}]
    assert json.loads((tmp_path / "bybit-mcp/release.json").read_text())["image"] == IMAGE + "@" + digest
    assert unrelated.read_text() == "preserve\n"
    promote(tmp_path, "sha256:" + "c" * 64, "d" * 40)
    config = yaml.safe_load((tmp_path / "bybit-mcp/kustomization.yaml").read_text())
    assert config["images"][0]["digest"] == "sha256:" + "c" * 64


@pytest.mark.parametrize("digest,commit", [("latest", "b" * 40), ("sha256:" + "a" * 64, "main")])
def test_gitops_rejects_mutable_image_or_unverified_source(tmp_path, digest, commit):
    with pytest.raises(ValueError):
        promote(tmp_path, digest, commit)
    assert not list(tmp_path.iterdir())
