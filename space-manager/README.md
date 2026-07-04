# iCloud Space Manager

A small web dashboard for **selectively deleting photos/videos from iCloud
after they are verifiably backed up locally** — the piece missing from
icloudpd and its web frontends. Built for the workflow: icloudpd downloads
everything to the NAS, Immich serves it, and you occasionally clear the
biggest space-hogs (long videos) out of iCloud.

It deliberately does **not** download anything. Pair it with your downloader
of choice (icloudpd / [icloudpd-web](https://github.com/AirswitchAsa/icloudpd-web) /
this repo's container).

## What it does

- Multi-account (e.g. yours and your spouse's), each with its own iCloud
  session and backup folder.
- Scans the iCloud **Videos** smart album (or the whole library) and caches
  filename / size / date per asset.
- Sortable, filterable table: largest first, videos only, minimum size,
  older-than-N-days.
- **Backup verification gate**: an asset can only be selected for deletion if
  a byte-identical copy (same name — including icloudpd's dedup-suffix
  variants — and same size) exists in the local backup tree. Verification is
  re-run against a fresh directory index at the moment of deletion.
- Deletion uses the exact CloudKit call icloudpd uses (`isDeleted = 1` on the
  `CPLAsset` record): assets move to **Recently Deleted**, recoverable for
  ~30 days, and still count against quota until purged there.
- **Dry-run by default.** Set `SPACE_MANAGER_DRY_RUN=false` only after a
  dry-run shows exactly what you expect.
- Typed confirmation phrase, per-asset audit log, batch cap of 500.

## Safety model

1. Backup tree is mounted **read-only** into this container.
2. Nothing is deletable unless verified present-and-identical locally.
3. Dry-run default; live mode is an explicit env change.
4. iCloud's own Recently Deleted is the 30-day undo.
5. Keep ZFS snapshots on the backup dataset so the local copy has history too.

⚠️ Deleting from iCloud removes the asset from **all devices** signed into
that account (that's how iCloud Photos works). The local NAS copy becomes the
only copy — snapshot it.

## Running

See `../deploy/docker-compose.yaml` and `../deploy/RUNBOOK.md`. Quick start:

```bash
cd space-manager
docker build -t icloud-space-manager .
mkdir -p data && cp accounts.example.toml data/accounts.toml  # then edit
docker run -p 8090:8090 \
  -v ./data:/data \
  -v "/mnt/Greg/Photo Backup:/backup:ro" \
  -e SPACE_MANAGER_UI_PASSWORD=change-me \
  icloud-space-manager
```

Open http://host:8090, connect the account (password + 2FA code on first
run; the session cookie then lasts ~2 months, same as icloudpd), scan, filter,
select, delete.

Notes:
- Requires Advanced Data Protection **disabled** on the Apple account (same
  limitation as icloudpd).
- Use an app-specific password if you prefer; the password is held in memory
  only, never written to disk (the session cookie is persisted in `/data`).
- Put the UI behind Tailscale/LAN only; `SPACE_MANAGER_UI_PASSWORD` adds
  HTTP basic auth as a second layer.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
pytest tests/
SPACE_MANAGER_CONFIG=./data/accounts.toml uvicorn --factory app.main:create_app --port 8090
```
