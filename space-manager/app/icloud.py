"""iCloud session handling, library scanning, and deletion.

Built on pyicloud_ipd, which ships inside the icloudpd package — the same
library icloudpd itself uses, so auth cookies, listing behaviour, and the
delete call match icloudpd exactly. Deleting sets isDeleted=1 on the CPLAsset
record, which moves the asset to iCloud's "Recently Deleted" (recoverable for
~30 days, and still counted against quota until purged).
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from typing import Any, Callable

from pyicloud_ipd.base import PyiCloudService
from pyicloud_ipd.exceptions import PyiCloudException
from pyicloud_ipd.item_type import AssetItemType
from pyicloud_ipd.services.photos import PhotoAsset, PhotoLibrary

from .config import Account

logger = logging.getLogger(__name__)

SCANNABLE_ALBUMS = ("Videos", "All Photos")


class AccountBusyError(Exception):
    """The account's iCloud session is busy (e.g. a scan is running)."""


class NotReadyError(Exception):
    """The account is not authenticated yet."""


def build_delete_payload(
    asset_record_name: str, asset_change_tag: str, zone_id: dict[str, Any]
) -> dict[str, Any]:
    """CloudKit records/modify body that moves one asset to Recently Deleted.

    Field-for-field identical to icloudpd's delete_photo().
    """
    return {
        "atomic": True,
        "desiredKeys": ["isDeleted"],
        "operations": [
            {
                "operationType": "update",
                "record": {
                    "fields": {"isDeleted": {"value": 1}},
                    "recordChangeTag": asset_change_tag,
                    "recordName": asset_record_name,
                    "recordType": "CPLAsset",
                },
            }
        ],
        "zoneID": zone_id,
    }


def asset_to_row(photo: PhotoAsset) -> dict[str, Any] | None:
    """Extract the fields we persist from a PhotoAsset. None if unusable."""
    try:
        size = photo.size
    except (KeyError, TypeError):
        return None  # no original resource metadata; can't verify safely
    try:
        filename = photo.filename
    except Exception:  # filenameEnc can be malformed; fall back to record id
        filename = f"{photo.id}.{photo.item_type_extension}"
    item_type = photo.item_type
    return {
        "master_id": photo.id,
        "asset_record_name": photo._asset_record["recordName"],
        "asset_change_tag": photo._asset_record["recordChangeTag"],
        "filename": filename,
        "size": size,
        "asset_date": photo.asset_date.isoformat(),
        "item_type": (
            "movie"
            if item_type == AssetItemType.MOVIE
            else "image"
            if item_type == AssetItemType.IMAGE
            else "unknown"
        ),
    }


class AccountSession:
    """One iCloud session per configured account, serialized by a lock."""

    def __init__(self, account: Account):
        self.account = account
        self.lock = threading.Lock()
        self._icloud: PyiCloudService | None = None
        self.last_error: str | None = None
        self.scan_state: dict[str, Any] = {
            "running": False,
            "album": None,
            "count": 0,
            "skipped": 0,
            "error": None,
        }

    # -- auth ---------------------------------------------------------------

    @property
    def status(self) -> str:
        if self._icloud is None:
            return "disconnected"
        if self._icloud.requires_2fa:
            return "needs_2fa"
        if "webservices" not in self._icloud.data:
            return "needs_password"
        return "ready"

    def connect(self, password: str | None = None) -> str:
        """(Re)connect. Without a password this only succeeds if the stored
        session cookie is still valid; otherwise status becomes needs_password."""
        with self.lock:
            self.last_error = None
            provider: Callable[[], str | None] = lambda: password
            try:
                self._icloud = PyiCloudService(
                    self.account.domain,
                    self.account.apple_id,
                    provider,
                    cookie_directory=self.account.cookie_directory,
                )
            except PyiCloudException as exc:
                self._icloud = None
                self.last_error = str(exc)
                logger.warning("connect failed for %s: %s", self.account.name, exc)
            return self.status

    def submit_2fa(self, code: str) -> str:
        with self.lock:
            if self._icloud is None:
                raise NotReadyError("connect first")
            self.last_error = None
            try:
                ok = self._icloud.validate_2fa_code(code)
            except PyiCloudException as exc:
                self.last_error = str(exc)
                return self.status
            if not ok:
                self.last_error = "2FA code rejected"
            return self.status

    def _library(self) -> PhotoLibrary:
        if self._icloud is None or self.status != "ready":
            raise NotReadyError(f"account {self.account.name} is not authenticated")
        return self._icloud.photos  # primary library (PrimarySync zone)

    # -- scanning -----------------------------------------------------------

    def start_scan(self, album: str, store: Any) -> None:
        if album not in SCANNABLE_ALBUMS:
            raise ValueError(f"album must be one of {SCANNABLE_ALBUMS}")
        if self.scan_state["running"]:
            raise AccountBusyError("a scan is already running")
        self._library()  # raises NotReadyError early, before the thread starts
        self.scan_state = {
            "running": True,
            "album": album,
            "count": 0,
            "skipped": 0,
            "error": None,
        }
        thread = threading.Thread(
            target=self._scan_worker, args=(album, store), daemon=True
        )
        thread.start()

    def _scan_worker(self, album: str, store: Any) -> None:
        account = self.account.name
        try:
            with self.lock:
                library = self._library()
                photo_album = library.all if album == "All Photos" else library.albums[album]
                store.begin_scan(account, album)
                batch: list[dict[str, Any]] = []
                for photo in photo_album:
                    row = asset_to_row(photo)
                    if row is None:
                        self.scan_state["skipped"] += 1
                        continue
                    batch.append(row)
                    if len(batch) >= 200:
                        self.scan_state["count"] += store.add_assets(account, album, batch)
                        batch = []
                if batch:
                    self.scan_state["count"] += store.add_assets(account, album, batch)
                store.finish_scan(account)
        except Exception as exc:  # surfaced via scan status endpoint
            logger.exception("scan failed for %s", account)
            self.scan_state["error"] = str(exc)
        finally:
            self.scan_state["running"] = False

    # -- deletion -----------------------------------------------------------

    def _fresh_change_tag(self, library: PhotoLibrary, record_name: str) -> str | None:
        """Look up the record's current change tag; None if lookup fails.
        Scans can be days old and a stale tag makes records/modify fail."""
        try:
            url = (
                f"{library.service_endpoint}/records/lookup?"
                f"{urllib.parse.urlencode(library.params)}"
            )
            body = json.dumps(
                {"records": [{"recordName": record_name}], "zoneID": library.zone_id}
            )
            response = library.session.post(
                url, data=body, headers={"Content-type": "application/json"}
            ).json()
            records = response.get("records", [])
            if records and not records[0].get("serverErrorCode"):
                return records[0].get("recordChangeTag")
        except Exception as exc:
            logger.warning("record lookup failed for %s: %s", record_name, exc)
        return None

    def delete_assets(
        self, rows: list[dict[str, Any]], dry_run: bool
    ) -> list[dict[str, Any]]:
        """Move the given assets (rows from the store) to Recently Deleted.

        Returns one result dict per row: {master_id, filename, ok, detail}.
        """
        if self.scan_state["running"]:
            raise AccountBusyError("wait for the running scan to finish")
        results = []
        with self.lock:
            library = self._library()
            for row in rows:
                if dry_run:
                    results.append(
                        {
                            "master_id": row["master_id"],
                            "filename": row["filename"],
                            "ok": True,
                            "detail": "dry-run: would delete",
                        }
                    )
                    continue
                try:
                    tag = (
                        self._fresh_change_tag(library, row["asset_record_name"])
                        or row["asset_change_tag"]
                    )
                    url = (
                        f"{library.service_endpoint}/records/modify?"
                        f"{urllib.parse.urlencode(library.params)}"
                    )
                    payload = build_delete_payload(
                        row["asset_record_name"], tag, library.zone_id
                    )
                    response = library.session.post(
                        url,
                        data=json.dumps(payload),
                        headers={"Content-type": "application/json"},
                    ).json()
                    record = (response.get("records") or [{}])[0]
                    error = record.get("serverErrorCode")
                    if error:
                        reason = record.get("reason", "")
                        results.append(
                            {
                                "master_id": row["master_id"],
                                "filename": row["filename"],
                                "ok": False,
                                "detail": f"{error} {reason}".strip(),
                            }
                        )
                    else:
                        results.append(
                            {
                                "master_id": row["master_id"],
                                "filename": row["filename"],
                                "ok": True,
                                "detail": "moved to Recently Deleted",
                            }
                        )
                except Exception as exc:
                    logger.exception("delete failed for %s", row["filename"])
                    results.append(
                        {
                            "master_id": row["master_id"],
                            "filename": row["filename"],
                            "ok": False,
                            "detail": str(exc),
                        }
                    )
        return results
