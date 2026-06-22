"""app/core/runtime/cloud_syncer.py — ETag-aware S3 + Supabase tool-file sync.

Pulls tool source files from cloud storage onto the local ``data/`` tree, then
triggers a plugin reload. Supabase is the primary target (where the Qt client
uploads); S3 is the secondary/legacy path.

Every method here is synchronous and blocking by design — it is meant to run in
the shared thread pool (see ``app/core/runtime/threadpool.py``), never directly on
the event loop. The Supabase calls therefore use the *sync* httpx API on purpose;
do not swap them for the pooled async client.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any

try:
    import boto3 as _boto3  # noqa: F401
    S3_AVAILABLE = True
except ImportError:
    _boto3 = None  # type: ignore
    S3_AVAILABLE = False

from app.config import settings
from app.core.runtime.plugin_loader import _get_data_root, reload_plugins

logger = logging.getLogger(__name__)


class _S3Syncer:
    def __init__(self) -> None:
        self._client: Any = None
        self._creds: tuple[str, str] = ("", "")
        self._uid_cache: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    @property
    def _etag_path(self) -> Path:
        p = _get_data_root()
        p.mkdir(exist_ok=True)
        return p / ".s3_etag_cache.json"

    def _load_etags(self) -> dict:
        if self._etag_path.exists():
            try:
                return json.loads(self._etag_path.read_text())
            except Exception as exc:
                logger.warning("Could not read S3 etag cache: %s", exc)
                return {}
        return {}

    def _save_etags(self, cache: dict) -> None:
        tmp = self._etag_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(cache))
            tmp.replace(self._etag_path)
        except Exception as exc:
            logger.warning("Could not write S3 etag cache: %s", exc)

    def _get_client(self) -> Any:
        if _boto3 is None:
            raise RuntimeError("boto3 not installed")
        key = (
            os.environ.get("AWS_ACCESS_KEY_ID", ""),
            os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        )
        if self._client is None or key != self._creds:
            self._client = _boto3.client("s3", aws_access_key_id=key[0], aws_secret_access_key=key[1])
            self._creds = key
        return self._client

    def lookup_user_id(self, username: str, project: str | None = None) -> str:
        cache_key = (username, project or "")
        if cache_key in self._uid_cache:
            return self._uid_cache[cache_key]
        uid = "10"
        try:
            s3 = self._get_client()
            bucket = settings.s3_bucket_name
            pattern = f"{username}/{project}/" if project else f"{username}/"
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix="users/"):
                for obj in page.get("Contents", []):
                    if pattern in obj["Key"]:
                        parts = obj["Key"].split("/")
                        if len(parts) >= 2 and parts[0] == "users":
                            uid = parts[1]
                            break
                else:
                    continue
                break
        except Exception as exc:
            logger.warning("S3 user-id lookup failed for %s: %s", username, exc)
        self._uid_cache[cache_key] = uid
        return uid

    def sync_all(
        self,
        username: str | None = None,
        project: str | None = None,
        clean_first: bool = False,
    ) -> dict[str, Any]:
        if not S3_AVAILABLE:
            return {"success": False, "error": "boto3 not installed"}
        if not os.environ.get("AWS_ACCESS_KEY_ID"):
            return {"success": False, "error": "AWS credentials not configured"}
        try:
            s3 = self._get_client()
            bucket = settings.s3_bucket_name
            data_dir = _get_data_root()
            data_dir.mkdir(exist_ok=True)

            paginator = s3.get_paginator("list_objects_v2")
            all_objs: list = []
            for page in paginator.paginate(Bucket=bucket, Prefix="users/"):
                all_objs.extend(page.get("Contents", []))

            if not all_objs:
                return {"success": True, "files_synced": 0, "message": "No files in S3"}

            if clean_first:
                target = data_dir / username if username else None
                if project and username:
                    target = data_dir / username / project
                if target and target.exists():
                    shutil.rmtree(target, ignore_errors=True)

            files_synced = files_skipped = files_deleted = 0
            s3_files: set[str] = set()

            with self._lock:
                etag_cache = self._load_etags()
                for obj in all_objs:
                    key = obj["Key"]
                    if key.endswith("/"):
                        continue
                    if username and username not in key:
                        continue
                    if project and project not in key:
                        continue
                    if "/tools/" not in key:
                        continue
                    parts = key.split("/")
                    local_key = "/".join(parts[2:]) if len(parts) >= 3 and parts[0] == "users" else key
                    s3_files.add(local_key)
                    local_path = data_dir / local_key
                    s3_etag = obj.get("ETag", "").strip('"')
                    if etag_cache.get(key) == s3_etag and local_path.exists():
                        files_skipped += 1
                        continue
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        s3.download_file(bucket, key, str(local_path))
                        etag_cache[key] = s3_etag
                        files_synced += 1
                    except Exception as exc:
                        logger.error("S3 download failed %s: %s", key, exc)

                # Delete orphans
                for tp in data_dir.rglob("tools"):
                    if not tp.is_dir():
                        continue
                    for lf in tp.rglob("*"):
                        if lf.is_file():
                            try:
                                lf_key = lf.relative_to(data_dir).as_posix()
                                if lf_key not in s3_files:
                                    lf.unlink()
                                    files_deleted += 1
                            except Exception as exc:
                                logger.debug("Could not remove orphan file %s: %s", lf, exc)

                self._save_etags(etag_cache)

            tools_loaded = 0
            if files_synced or files_deleted:
                result = reload_plugins()
                tools_loaded = result.get("tools", 0)

            return {
                "success": True,
                "files_synced": files_synced,
                "files_skipped": files_skipped,
                "files_deleted": files_deleted,
                "tools_loaded": tools_loaded,
            }
        except Exception as exc:
            logger.exception("S3 sync_all failed: %s", exc)
            return {"success": False, "error": str(exc)}

    def sync_tool(
        self,
        username: str,
        project: str,
        tool_name: str,
        category: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        if not S3_AVAILABLE:
            return {"success": False, "error": "boto3 not installed"}
        if not os.environ.get("AWS_ACCESS_KEY_ID"):
            return {"success": False, "error": "AWS credentials not configured"}
        try:
            s3 = self._get_client()
            bucket = settings.s3_bucket_name
            data_dir = _get_data_root()
            uid = user_id or self.lookup_user_id(username, project)
            prefix = (
                f"users/{uid}/{username}/{project}/tools/{category}/{tool_name}/"
                if category
                else f"users/{uid}/{username}/{project}/tools/{tool_name}/"
            )
            files_synced = files_skipped = 0
            paginator = s3.get_paginator("list_objects_v2")
            with self._lock:
                etag_cache = self._load_etags()
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        if key.endswith("/"):
                            continue
                        parts = key.split("/", 2)
                        local_key = parts[2] if len(parts) == 3 else key
                        local_path = data_dir / local_key
                        s3_etag = obj.get("ETag", "").strip('"')
                        if etag_cache.get(key) == s3_etag and local_path.exists():
                            files_skipped += 1
                            continue
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            s3.download_file(bucket, key, str(local_path))
                            etag_cache[key] = s3_etag
                            files_synced += 1
                        except Exception as exc:
                            logger.error("S3 single-tool download failed %s: %s", key, exc)
                self._save_etags(etag_cache)

            tools_loaded = 0
            if files_synced:
                result = reload_plugins()
                tools_loaded = result.get("tools", 0)

            return {"success": True, "files_synced": files_synced, "files_skipped": files_skipped, "tools_loaded": tools_loaded}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def sync_tool_from_supabase(
        self,
        username: str,
        project: str,
        tool_name: str,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Pull tool files from Supabase storage (primary client upload target) and reload plugins.

        Supabase path layout: {username}/{project}/tools/{category}/{tool_name}/...
        Local layout:         data/{username}/{project}/tools/{category}/{tool_name}/...
        """
        import httpx as _httpx

        supabase_url = settings.supabase_url
        service_key = settings.supabase_service_role_key
        bucket = settings.supabase_storage_bucket

        if not supabase_url or not service_key:
            return {"success": False, "error": "Supabase credentials not configured on MCP server (set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)"}

        auth_headers = {"Authorization": f"Bearer {service_key}"}
        data_dir = _get_data_root()

        def _prefixes_to_try() -> list[str]:
            if category:
                return [f"{username}/{project}/tools/{category}/{tool_name}"]
            candidates = [
                f"{username}/{project}/tools/{tool_name}",
            ]
            # Discover category folders (e.g. general) when path omits category
            try:
                resp = _httpx.post(
                    f"{supabase_url}/storage/v1/object/list/{bucket}",
                    headers={**auth_headers, "Content-Type": "application/json"},
                    json={
                        "prefix": f"{username}/{project}/tools/",
                        "limit": 1000,
                        "offset": 0,
                    },
                    timeout=30.0,
                )
                if resp.status_code == 200:
                    for item in resp.json():
                        cat = item.get("name", "")
                        if not cat or cat.startswith(".") or item.get("id") is not None:
                            continue
                        candidates.append(
                            f"{username}/{project}/tools/{cat}/{tool_name}"
                        )
            except Exception as exc:
                logger.warning("Supabase category list failed: %s", exc)
            # Deduplicate while preserving order
            seen: set[str] = set()
            ordered: list[str] = []
            for p in candidates:
                if p not in seen:
                    seen.add(p)
                    ordered.append(p)
            return ordered

        def _list_recursive(path_prefix: str) -> list[str]:
            """Return flat list of all file paths under path_prefix in Supabase."""
            all_paths: list[str] = []
            try:
                resp = _httpx.post(
                    f"{supabase_url}/storage/v1/object/list/{bucket}",
                    headers={**auth_headers, "Content-Type": "application/json"},
                    json={"prefix": path_prefix, "limit": 1000, "offset": 0},
                    timeout=30.0,
                )
                if resp.status_code != 200:
                    return []
                for item in resp.json():
                    name = item.get("name", "")
                    if not name or name.startswith("."):
                        continue
                    child = f"{path_prefix}/{name}"
                    if item.get("id") is None:
                        all_paths.extend(_list_recursive(child))
                    else:
                        all_paths.append(child)
            except Exception as exc:
                logger.error("Supabase list failed for %s: %s", path_prefix, exc)
            return all_paths

        try:
            file_paths: list[str] = []
            for prefix in _prefixes_to_try():
                found = _list_recursive(prefix)
                if found:
                    file_paths = found
                    break

            files_synced = 0
            for file_path in file_paths:
                filename = file_path.rsplit("/", 1)[-1]
                if not (filename.endswith(".py") or filename.endswith(".txt") or filename.endswith(".json")):
                    continue
                try:
                    dl = _httpx.get(
                        f"{supabase_url}/storage/v1/object/{bucket}/{file_path}",
                        headers=auth_headers,
                        timeout=30.0,
                    )
                    if dl.status_code != 200:
                        logger.warning("Supabase download failed %s: HTTP %s", file_path, dl.status_code)
                        continue
                except Exception as exc:
                    logger.error("Supabase download error %s: %s", file_path, exc)
                    continue

                local_path = data_dir / file_path
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_bytes(dl.content)
                files_synced += 1

            tools_loaded = 0
            if files_synced > 0:
                result = reload_plugins()
                tools_loaded = result.get("tools", 0)

            return {"success": True, "files_synced": files_synced, "tools_loaded": tools_loaded}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


s3_syncer = _S3Syncer()
