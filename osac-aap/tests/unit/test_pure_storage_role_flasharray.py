"""Unit tests for the FlashArray File mode of the pure_storage role.

This file duplicates the small parsing helpers from
test_pure_storage_role.py on purpose: the FlashBlade tests assert the
production path and must keep passing unchanged while the test-only
FlashArray branch evolves (see tests/manual/pure/flasharray-file-plan.md).
"""

import json
import secrets
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from ansible.parsing.dataloader import DataLoader  # type: ignore[import-not-found]
from ansible.template import Templar  # type: ignore[import-not-found]

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Redacted(str):
    """A string that compares and encodes like its value but never prints it."""

    def __repr__(self) -> str:
        return "'<redacted>'"


FIXTURE_TOKEN = _Redacted(secrets.token_hex(16))
PURE_ROLE_DIR = (
    REPO_ROOT
    / "collections"
    / "ansible_collections"
    / "osac"
    / "templates"
    / "roles"
    / "pure_storage"
)


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _find_task(tasks: list[dict], name: str) -> dict:
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("name") == name:
            return task
        for section in ("block", "rescue", "always"):
            nested = task.get(section)
            if not isinstance(nested, list):
                continue
            try:
                return _find_task(nested, name)
            except AssertionError:
                pass
    raise AssertionError(f"Task {name!r} not found")


def _render(expression, variables: dict):
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(expression)


def _as_json(rendered):
    return rendered if isinstance(rendered, dict) else json.loads(rendered)


def test_defaults_declare_flasharray_mode():
    """FlashArray mode stays opt-in and keeps its naming knobs in defaults."""
    defaults = _load_yaml(PURE_ROLE_DIR / "defaults" / "main.yaml")

    assert defaults["pure_storage_backend_type"] == "flashblade"
    assert defaults["pure_storage_fa_file_system_prefix"] == "osac-"
    assert defaults["pure_storage_fa_nfs_policy"] == ""
    assert defaults["pure_storage_fa_nfs_server"] == ""
    assert defaults["pure_storage_csi_provisioner"] == "pxd.portworx.com"


def test_flashblade_realm_setup_is_gated():
    """The Realm-pool checkout must not run for FlashArray dispatches."""
    tasks = _load_yaml(PURE_ROLE_DIR / "tasks" / "setup.yaml")

    realm_task = _find_task(tasks, "Provision Pure Storage Realm for tenant")
    assert realm_task["when"] == "pure_storage_backend_type == 'flashblade'"


def test_flasharray_setup_writes_hub_secret():
    """FlashArray setup records the array-wide allocation without a Realm."""
    tasks = _load_yaml(PURE_ROLE_DIR / "tasks" / "setup.yaml")

    fa_task = _find_task(tasks, "Provision Pure Storage FlashArray File for tenant")
    assert fa_task["when"] == "pure_storage_backend_type == 'flasharray'"

    secret_task = _find_task(
        fa_task["block"], "Create hub Secret recording tenant FlashArray allocation"
    )
    definition = secret_task["kubernetes.core.k8s"]["definition"]
    variables = {
        "pure_storage_tenant_config_secret_prefix": "pure-tenant-config-",
        "pure_storage_config_namespace": "osac-system",
        "tenant_name": "test-tenant",
        "tenant_uid": "0123456789abcdef",
        "_pure_backend_id": "be-fa-test",
        "_pure_nfs_server": "fa-server",
        "_pure_nfs_policy": "fa-policy",
        "_pure_fa_file_system": "osac-test-tenant",
        "_pure_api_token": FIXTURE_TOKEN,
        "_pure_mgmt_endpoint": "192.0.2.10",
        "_pure_nfs_endpoint": "192.0.2.20",
    }

    assert _render(definition["metadata"]["name"], variables) == (
        "pure-tenant-config-test-tenant-be-fa-test"
    )

    labels = _render(definition["metadata"]["labels"], variables)
    assert labels["osac.openshift.io/pure-backend-type"] == "flasharray"
    assert "osac.openshift.io/pure-realm" not in labels

    data = _render(definition["stringData"], variables)
    assert data["realm_name"] == ""
    assert data["fa_file_system"] == "osac-test-tenant"
    assert data["nfs_server"] == "fa-server"
    assert data["nfs_policy"] == "fa-policy"
    assert data["storage_provider_type"] == "pure"


def test_flasharray_storageclass_parameters():
    """FlashArray classes use pure_fa_file and the file system parameter."""
    tasks = _load_yaml(PURE_ROLE_DIR / "tasks" / "ensure_storage_class.yaml")

    sc_task = _find_task(tasks, "Create StorageClass per tier (FlashArray)")
    assert "pure_storage_backend_type == 'flasharray'" in sc_task["when"]

    variables = {
        "pure_storage_csi_provisioner": "pxd.portworx.com",
        "pure_storage_nfs_version": "nfsvers=4.1",
        "_pure_nfs_server": "fa-server",
        "_pure_nfs_policy": "fa-policy",
        "_pure_fa_file_system": "osac-test-tenant",
        "_pure_csi_secret_name": "pure-csi-secret-be-fa-test",
        "tenant_namespace": "test-tenant-ns",
        "tenant_name": "test-tenant",
        "_pure_backend_id": "be-fa-test",
        "item": {"name": "default", "protocol": "nfs"},
    }

    definition = sc_task["kubernetes.core.k8s"]["definition"]
    assert _render(definition["provisioner"], variables) == "pxd.portworx.com"

    params = _render(definition["parameters"], variables)
    assert params["backend"] == "pure_fa_file"
    assert params["pure_fa_file_system"] == "osac-test-tenant"
    assert params["pure_nfs_server"] == "fa-server"
    assert params["pure_nfs_policy"] == "fa-policy"
    assert "pure_fb_hard_limit_enabled" not in params
    assert params["csi.storage.k8s.io/provisioner-secret-name"] == (
        "pure-csi-secret-be-fa-test"
    )
    assert params["csi.storage.k8s.io/provisioner-secret-namespace"] == (
        "test-tenant-ns"
    )

    assert _render(definition["mountOptions"], variables) == [
        "nfsvers=4.1",
        "tcp",
    ]
    assert _render(definition["volumeBindingMode"], variables) == "Immediate"


def test_flasharray_csi_secret_uses_flasharrays():
    """The FlashArray secret carries a FlashArrays entry, not FlashBlades."""
    tasks = _load_yaml(PURE_ROLE_DIR / "tasks" / "ensure_storage_class.yaml")

    secret_task = _find_task(tasks, "Create pure.json Secret for FlashArray CSI driver")
    assert secret_task["when"] == "pure_storage_backend_type == 'flasharray'"

    variables = {
        "_pure_mgmt_endpoint": "192.0.2.10",
        "_pure_nfs_endpoint": "192.0.2.20",
        "_pure_api_token": FIXTURE_TOKEN,
    }
    rendered = _render(
        secret_task["kubernetes.core.k8s"]["definition"]["stringData"]["pure.json"],
        variables,
    )
    pure_json = _as_json(rendered)

    assert "FlashArrays" in pure_json
    assert "FlashBlades" not in pure_json
    assert len(pure_json["FlashArrays"]) == 1
    array = pure_json["FlashArrays"][0]
    assert array["MgmtEndPoint"] == "192.0.2.10"
    assert array["NFSEndPoint"] == "192.0.2.20"
    assert "APIToken" in array


def test_flashblade_csi_secret_task_is_preserved():
    """The FlashBlade secret task keeps its name and gains a guard."""
    tasks = _load_yaml(PURE_ROLE_DIR / "tasks" / "ensure_storage_class.yaml")

    secret_task = _find_task(tasks, "Create pure.json Secret for CSI driver")
    assert secret_task["when"] == "pure_storage_backend_type == 'flashblade'"


def test_pure_csi_secret_role_branches_on_backend_type():
    """ensure_pure_csi_secret builds the right array key per backend type."""
    tasks = _load_yaml(PURE_ROLE_DIR / "tasks" / "ensure_pure_csi_secret.yaml")

    fb_task = _find_task(tasks, "Build FlashBlades array for pure.json")
    fa_task = _find_task(tasks, "Build FlashArrays array for pure.json")
    assert fb_task["when"] == "pure_storage_backend_type == 'flashblade'"
    assert fa_task["when"] == "pure_storage_backend_type == 'flasharray'"

    rendered = _render(
        fa_task["ansible.builtin.set_fact"]["_pure_flashblades_json"],
        {
            "_mgmt_endpoint": "192.0.2.10",
            "_api_token": FIXTURE_TOKEN,
            "_nfs_endpoint": "192.0.2.20",
        },
    )
    assert "FlashArrays" in rendered
    assert "FlashBlades" not in rendered


def test_flasharray_teardown_summary_is_gated():
    """Teardown reports the FlashArray allocation without realm language."""
    tasks = _load_yaml(PURE_ROLE_DIR / "tasks" / "teardown_backend.yaml")

    fa_task = _find_task(tasks, "Report FlashArray backend teardown summary")
    assert fa_task["when"] == "pure_storage_backend_type == 'flasharray'"
