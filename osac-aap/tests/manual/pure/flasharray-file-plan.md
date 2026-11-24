# FlashArray File Implementation Plan (Test-Only)

**Status:** Exploratory / Test-Only  
**Last Updated:** 2026-11-24  
**Context:** FlashArray File does NOT support creating file systems inside Realms (empirically verified 2026-11-23). This plan describes how to adapt the FlashBlade realm-pool model to work with FlashArray File for testing purposes only, accepting the lack of tenant isolation.

**Security Warning:** This approach gives every tenant an array-wide API token. A leaked CSI Secret exposes all tenants' data. Only suitable for single-tenant or mutually-trusting test environments.

---

## Phase 0 — Verify driver behavior (no code, ~2h)

Hand-write everything; keep AAP out of it.

1. **On the array:** Create an NFS export policy and a File Services data VIF/interface IP. Note whether a server object is needed (6.8.7+ multi-server support).

2. **Secret** — same two namespaces as today (osac-csi for controller, tenant namespace for node):
   ```json
   {
     "FlashArrays": [
       {
         "MgmtEndPoint": "<mgmt-vip>",
         "NFSEndPoint": "<file-vif>"
       }
     ]
   }
   ```

3. **StorageClass:**
   ```yaml
   provisioner: pxd.portworx.com
   parameters:
     backend: pure_fa_file
     pure_fa_file_system: osac-test-fs   # fs doesn't exist yet
     pure_nfs_policy: <policy-name>
     pure_nfs_server: <server-name>
   mountOptions: [nfsvers=4.1, tcp]
   ```

4. **Create a PVC.** Then inspect the array and driver logs:
   ```bash
   kubectl -n osac-csi-backends logs deployment/pure-csi-driver --tail=100
   ```

**Decision point:**
- **Outcome A** — A new file system appeared. Your assumption is correct: StorageClass is per-filesystem, provisioning is fully dynamic, and `pure_realm` has almost nothing to do (just ensure the export policy exists).
- **Outcome B** — A managed directory appeared inside `osac-test-fs` (or provisioning failed because the fs didn't exist). The file system is admin-provisioned state, not dynamic. The realm-pool model still works, but something must create the filesystem at onboarding — that becomes `pure_realm`'s job.

**Also record:** Does it need a pod? Does a pod at array root (outside any realm) work?

---

## Phase 1 — AAP role changes

Add `pure_storage_backend_type: flasharray` and branch on it rather than forking the role. The divergence is narrow — three files — and minimizes the maintenance surface for something explicitly throwaway.

### `defaults/main.yaml`
```yaml
pure_storage_backend_type: flashblade
pure_storage_csi_provisioner: pxd.portworx.com  # unchanged, same driver

# flasharray-only
pure_storage_fa_file_system_prefix: "osac-"
pure_storage_fa_nfs_policy: ""
pure_storage_fa_nfs_server: ""
```

The `nfs_server_suffix` / `export_policy_suffix` remain FlashBlade-only — they exist to build `realm::x` names that have no FA analogue.

### `tasks/setup.yaml`

Wrap the realm pool-checkout block with:
```yaml
when: pure_storage_backend_type == 'flashblade'
```

For FlashArray, skip pool checkout and write the hub Secret directly:

| Field | FlashArray |
|-------|------------|
| `realm_name` | omit, or the pod name if pods are required |
| `nfs_server` | server object name |
| `nfs_policy` | policy name |
| `fa_file_system` | `osac-<tenant>` |
| `api_token` / `mgmt_endpoint` / `nfs_endpoint` | same as FlashBlade |

**Note:** The existing validation around lines 103–124 hard-fails on empty `nfs_server`/`nfs_policy` — it needs the same `when: pure_storage_backend_type == 'flashblade'` guard.

### `tasks/ensure_storage_class.yaml`

Two changes:
- **CSI Secret JSON:** `FlashBlades` → `FlashArrays` (the `MgmtEndPoint`/`NFSEndPoint` structure exists in both)
- **StorageClass parameters:** `pure_file` → `pure_fa_file`, drop `pure_fb_hard_limit_enabled`, add `pure_fa_file_system`

### `tasks/ensure_pure_csi_secret.yaml`

If this exists as a separate include, it also has the `FlashBlades` literal; check and branch it too.

### `tasks/teardown_*.yaml`

Realm release is FlashBlade-only. If outcome B from Phase 0 applies, add filesystem deletion here.

### `meta/osac.yaml`

No change. FA File is still NFS, so dispatcher routing and `supported_protocols: [nfs]` hold.

### `osac.service.pure_realm`

**Only if outcome B.** Becomes "ensure file system + export policy exist," which is a much smaller role than realm provisioning (no Realm object, no service account, no token generation, no data VIF, no DNS). Might just be a few `purefa.flasharray.purefa_*` tasks.

---

## Phase 2 — CSI driver install

Expect no changes. FA File is NFS over TCP; the protocol is identical: no `PURE_FLASHARRAY_SAN_TYPE`, no iSCSI initiators, no multipath. Confirm in Phase 0 that the node DaemonSet starts and mounts succeed without modification — if it does, `pure-csi-manifests/` ships as-is.

**30-node.yaml stays load-bearing.** The iSCSI/multipath volumes and config are present but unused; removing them is not load-bearing for this test path.

---

## Phase 3 — Tests

### Unit tests

`tests/unit/test_pure_storage_role.py` currently asserts `pure_file`, `realm::`-qualified names, and the `export-policy` suffix. Rather than edit the FlashBlade assertions and add FA cases that set `pure_storage_backend_type`, **duplicate the file** to `test_pure_storage_role_flasharray.py`. The production path stays covered.

### Integration tests

`pure_storage_ensure_sc` and `pure_storage_multi_tenant` both seed realms via the mock. For FA you'd need a second mock speaking the FlashArray File API (`/file-systems`, `/directories`, `/directory-exports`).

**Recommendation:** Skip this unless FA becomes a supported backend; manual verification is proportionate for a test-only path.

---

## Sequencing

**Phase 0 alone may be all you need.** If the goal is just "verify a PVC works against an FA," it ends there — no AAP changes, no test churn.

**Phase 1 only earns its keep** if you want the full tenant onboarding flow driven by playbooks. For one-off testing, hand-written Secrets and StorageClasses (Phase 0 artifacts) are faster.

---

## Why This Is Test-Only

1. **No tenant isolation:** Every tenant gets an array-wide API token. One leaked Secret compromises all tenants.
2. **FlashArray refuses filesystems in realms:** Empirically verified error: `"<realm>::<pod>::<fs>: File system in realm is not supported"`
3. **No realm-pool model:** There's nothing to check out — file systems live at array root.
4. **Multi-tenant file on Pure = FlashBlade:** The existing realm-pool design is production-ready there.

For production multi-tenant file storage on Pure, use **FlashBlade** with the existing implementation.

For FlashArray in production OSAC deployments, use **block storage** (`backend: pure_block`) with realms+pods+SMT — but this requires iSCSI/multipath on CSI nodes.
