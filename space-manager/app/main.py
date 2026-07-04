"""FastAPI app for iCloud Space Manager.

Run with:  uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .config import Settings, load_settings
from .icloud import SCANNABLE_ALBUMS, AccountBusyError, AccountSession, NotReadyError
from .store import Store
from .verify import LocalIndex, build_index, find_local_copy

logging.basicConfig(level=os.environ.get("SPACE_MANAGER_LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

CONFIRM_PHRASE = "DELETE FROM ICLOUD"
INDEX_TTL_SECONDS = 15 * 60
MAX_DELETE_BATCH = 500
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class ConnectBody(BaseModel):
    password: str | None = None


class TwoFactorBody(BaseModel):
    code: str


class ScanBody(BaseModel):
    album: str = "Videos"


class DeleteBody(BaseModel):
    master_ids: list[str]
    confirm: str = ""


def _mask(apple_id: str) -> str:
    user, _, domain = apple_id.partition("@")
    if len(user) > 2:
        user = user[0] + "…" + user[-1]
    return f"{user}@{domain}" if domain else user


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.db_path)
    sessions: dict[str, AccountSession] = {
        acc.name: AccountSession(acc) for acc in settings.accounts
    }
    index_cache: dict[str, LocalIndex] = {}

    app = FastAPI(title="iCloud Space Manager", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.sessions = sessions

    # -- optional basic auth on everything ----------------------------------

    @app.middleware("http")
    async def basic_auth(request: Request, call_next: Any) -> Response:
        if settings.ui_password:
            header = request.headers.get("authorization", "")
            ok = False
            if header.startswith("Basic "):
                try:
                    decoded = base64.b64decode(header[6:]).decode()
                    _user, _, password = decoded.partition(":")
                    ok = secrets.compare_digest(password, settings.ui_password)
                except Exception:
                    ok = False
            if not ok:
                return Response(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="space-manager"'},
                )
        return await call_next(request)

    # -- helpers -------------------------------------------------------------

    def get_session(name: str) -> AccountSession:
        session = sessions.get(name)
        if session is None:
            raise HTTPException(404, f"unknown account: {name}")
        return session

    def get_index(name: str, refresh: bool = False) -> LocalIndex:
        session = get_session(name)
        cached = index_cache.get(name)
        if refresh or cached is None or time.time() - cached.built_at > INDEX_TTL_SECONDS:
            cached = build_index(session.account.backup_dirs)
            index_cache[name] = cached
        return cached

    # -- UI ------------------------------------------------------------------

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    # -- state / auth ---------------------------------------------------------

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        accounts = []
        for acc in settings.accounts:
            session = sessions[acc.name]
            index = index_cache.get(acc.name)
            accounts.append(
                {
                    "name": acc.name,
                    "apple_id": _mask(acc.apple_id),
                    "status": session.status,
                    "last_error": session.last_error,
                    "scan": {**session.scan_state, "info": store.scan_info(acc.name)},
                    "backup_dirs": list(acc.backup_dirs),
                    "index": (
                        {
                            "file_count": index.file_count,
                            "built_at": index.built_at,
                            "errors": index.errors,
                        }
                        if index
                        else None
                    ),
                }
            )
        return {
            "dry_run": settings.dry_run,
            "confirm_phrase": CONFIRM_PHRASE,
            "albums": list(SCANNABLE_ALBUMS),
            "accounts": accounts,
        }

    @app.post("/api/accounts/{name}/connect")
    async def connect(name: str, body: ConnectBody) -> dict[str, Any]:
        session = get_session(name)
        status = session.connect(body.password)
        return {"status": status, "last_error": session.last_error}

    @app.post("/api/accounts/{name}/2fa")
    async def two_factor(name: str, body: TwoFactorBody) -> dict[str, Any]:
        session = get_session(name)
        try:
            status = session.submit_2fa(body.code.strip())
        except NotReadyError as exc:
            raise HTTPException(409, str(exc))
        return {"status": status, "last_error": session.last_error}

    # -- scanning -------------------------------------------------------------

    @app.post("/api/accounts/{name}/scan")
    async def scan(name: str, body: ScanBody) -> dict[str, Any]:
        session = get_session(name)
        try:
            session.start_scan(body.album, store)
        except (NotReadyError, AccountBusyError) as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"started": True}

    @app.get("/api/accounts/{name}/scan")
    async def scan_status(name: str) -> dict[str, Any]:
        session = get_session(name)
        return {**session.scan_state, "info": store.scan_info(name)}

    # -- assets ----------------------------------------------------------------

    @app.get("/api/accounts/{name}/assets")
    async def assets(
        name: str,
        item_type: str | None = None,
        min_mb: float | None = None,
        older_than_days: int | None = None,
        q: str | None = None,
        sort: str = "size",
        limit: int = 500,
        offset: int = 0,
        refresh_index: bool = False,
    ) -> dict[str, Any]:
        get_session(name)
        index = get_index(name, refresh=refresh_index)
        rows = store.list_assets(
            name,
            item_type=item_type or None,
            min_size=int(min_mb * 1024 * 1024) if min_mb else None,
            older_than_days=older_than_days,
            name_contains=q or None,
            sort=sort,
            limit=min(limit, 2000),
            offset=offset,
        )
        for row in rows:
            local = find_local_copy(index, row["filename"], row["size"])
            row["verified"] = local is not None
            row["local_path"] = local
        return {
            "rows": rows,
            "index": {
                "file_count": index.file_count,
                "built_at": index.built_at,
                "errors": index.errors,
            },
        }

    # -- deletion ----------------------------------------------------------------

    @app.post("/api/accounts/{name}/delete")
    async def delete(name: str, body: DeleteBody) -> JSONResponse:
        session = get_session(name)
        if body.confirm != CONFIRM_PHRASE:
            raise HTTPException(400, f'confirmation phrase must be "{CONFIRM_PHRASE}"')
        if not body.master_ids:
            raise HTTPException(400, "no assets selected")
        if len(body.master_ids) > MAX_DELETE_BATCH:
            raise HTTPException(400, f"at most {MAX_DELETE_BATCH} assets per batch")

        rows = store.get_assets(name, body.master_ids)
        found = {r["master_id"] for r in rows}
        missing = [m for m in body.master_ids if m not in found]
        if missing:
            raise HTTPException(
                400, f"{len(missing)} selected assets are not in the scan cache; rescan first"
            )

        # Always verify against a freshly built local index at delete time.
        index = build_index(session.account.backup_dirs)
        index_cache[name] = index
        if index.errors:
            raise HTTPException(409, f"backup dirs unavailable: {index.errors}")
        unverified = [
            r["filename"]
            for r in rows
            if find_local_copy(index, r["filename"], r["size"]) is None
        ]
        if unverified:
            raise HTTPException(
                409,
                "refusing: no verified local copy for: " + ", ".join(unverified[:20]),
            )

        dry_run = settings.dry_run
        try:
            results = session.delete_assets(rows, dry_run=dry_run)
        except (NotReadyError, AccountBusyError) as exc:
            raise HTTPException(409, str(exc))

        sizes = {r["master_id"]: r["size"] for r in rows}
        freed = 0
        deleted_ids = []
        for result in results:
            action = "dry_run_delete" if dry_run else "delete"
            outcome = ("ok: " if result["ok"] else "error: ") + result["detail"]
            store.audit(
                name,
                result["master_id"],
                result["filename"],
                sizes.get(result["master_id"], 0),
                action,
                outcome,
            )
            if result["ok"] and not dry_run:
                freed += sizes.get(result["master_id"], 0)
                deleted_ids.append(result["master_id"])
        store.remove_assets(name, deleted_ids)

        return JSONResponse(
            {
                "dry_run": dry_run,
                "results": results,
                "freed_bytes": freed,
                "note": (
                    "Dry-run mode is ON (SPACE_MANAGER_DRY_RUN); nothing was deleted."
                    if dry_run
                    else "Deleted assets sit in iCloud's Recently Deleted for ~30 days "
                    "and still count against quota until purged."
                ),
            }
        )

    # -- audit ------------------------------------------------------------------

    @app.get("/api/accounts/{name}/audit")
    async def audit(name: str, limit: int = 200) -> dict[str, Any]:
        get_session(name)
        return {"entries": store.audit_entries(name, limit=min(limit, 1000))}

    return app
