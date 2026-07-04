# iCloud Photos → TrueNAS backup runbook

The workflow this stack implements:

```
iPhones (2 accounts, Optimize Storage ON)
        │  iCloud Photos
        ▼
icloudpd-web  ──nightly──▶  /mnt/Greg/Photo Backup/icloud_{joseph,christine}
                                   │                    ▲
                                   │ read-only          │ ZFS snapshots (undo)
                                   ▼                    │
                     Immich external library     space-manager verifies here,
                     (browse/search over            then deletes selected
                      Tailscale)                    large files from iCloud
```

Prerequisites: Advanced Data Protection **off** on both Apple IDs (icloudpd
limitation); expect Apple to expire each session cookie roughly every
2 months — both UIs let you re-enter the 2FA code in the browser.

## Phase 1 — downloads (icloudpd-web)

1. `docker compose up -d icloudpd-web`, open `http://nas:5000`.
2. Create one **policy per account**:
   - directory `/downloads/icloud_joseph` (resp. `_christine`)
   - authenticate (password + 2FA)
   - schedule nightly (cron, e.g. `0 3 * * *`)
3. Let the initial full sync finish (large libraries take days; progress is
   visible in the UI, and interrupted runs resume on the next scheduled run).

## Phase 2 — protect the backup

On TrueNAS, add a **periodic snapshot task** on the dataset holding
`Photo Backup` (e.g. daily, retain 4 weeks). This is the undo button for
anything that ever goes wrong locally after files stop existing in iCloud.

## Phase 3 — Immich (optional but recommended)

Add `/mnt/Greg/Photo Backup` to Immich as an **external library**, one per
person, mounted **read-only**. Immich indexes/serves the archive without
owning or modifying it. (Keep the Immich mobile-app backup off, or accept
that recent photos appear twice until you pick one ingestion path.)

## Phase 4 — freeing iCloud space (space-manager)

1. `docker compose up -d space-manager`, open `http://nas:8090`
   (`SPACE_MANAGER_UI_PASSWORD` protects it; keep it LAN/Tailscale-only).
2. Copy `space-manager/accounts.example.toml` to
   `/mnt/FastSSD/docker/icloud-space-manager/accounts.toml` and edit.
3. Connect the account, scan the **Videos** album, filter (e.g. ≥ 500 MB,
   older than 90 days), and review the *Backup* column — only assets with a
   verified byte-identical local copy can be selected.
4. With `SPACE_MANAGER_DRY_RUN=true` (default), run your first delete and
   check the audit log — nothing is actually removed.
5. When satisfied: set `SPACE_MANAGER_DRY_RUN=false` in the compose env,
   `docker compose up -d space-manager`, repeat, and type the confirmation
   phrase.
6. Space is reclaimed after items purge from **Recently Deleted** (~30 days),
   or immediately if you empty Recently Deleted from a device/icloud.com.

### What deletion means

- The asset disappears from iCloud **and every device** on that account
  (including the phone's optimized thumbnail).
- For ~30 days it is recoverable in Photos → Recently Deleted.
- After that, the NAS copy (plus its ZFS snapshots) is the only copy.

## Ongoing operation

| Cadence | Action |
|---|---|
| nightly | icloudpd-web scheduled sync (automatic) |
| ~2 months | re-auth both accounts when Apple expires the cookies (both UIs prompt) |
| as needed | space-manager: rescan → filter → verify → delete |
| automatic | ZFS snapshots on the backup dataset |

## Troubleshooting

- **Sync stalls / auth loop** → the MFA cookie expired; re-authenticate from
  the icloudpd-web UI (this was the usual cause of icloudpd "halting").
- **space-manager shows "not in backup" for files that exist** → the
  downloader may not have finished, converted HEIC without keeping the
  original, or renamed with a different policy; the gate matches
  `NAME.EXT`, `NAME-<bytes>.EXT`, `NAME-original.EXT` with exact byte size.
- **Delete returns a conflict error** → the library changed since the scan;
  rescan and retry (the app already refreshes each record's change tag at
  delete time, so this should be rare).
