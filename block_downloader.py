"""
block_downloader.py — ETag-aware S3 block downloader.

BlockDownloader manages the S3 client, user-id resolution cache, and ETag
change-detection cache so tool blocks are only downloaded when their content
has actually changed.  The module-level ``block_downloader`` singleton is
created here after the ``bridge`` singleton is available.
"""

import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    import boto3
except ImportError:
    boto3 = None  # type: ignore

from config import S3_AVAILABLE, _get_project_root
from bridge import bridge


class BlockDownloader:
    """Manages ETag-aware downloads of tool blocks from S3.

    Encapsulates the S3 client, user-id lookup cache, ETag change-detection
    cache, and both the full-bucket and single-tool sync operations.
    Thread-safe: sync_all and sync_tool may be called concurrently from the
    periodic background task and REST endpoint handlers.
    """

    def __init__(self, bucket: str, data_dir: Path, bridge: "Any") -> None:
        self._bucket = bucket
        self._data_dir = data_dir
        self._bridge = bridge
        self._client: Optional[Any] = None
        self._client_creds: Tuple[str, str] = ("", "")
        self._user_id_cache: Dict[Tuple[str, str], str] = {}
        self._etag_lock = threading.Lock()

    # ── S3 client ─────────────────────────────────────────────────────────────

    def get_client(self) -> Any:
        """Return a cached boto3 S3 client, rebuilding if credentials changed."""
        key: Tuple[str, str] = (
            os.environ.get('AWS_ACCESS_KEY_ID', ''),
            os.environ.get('AWS_SECRET_ACCESS_KEY', ''),
        )
        if self._client is None or key != self._client_creds:
            self._client = boto3.client(
                's3',
                aws_access_key_id=key[0],
                aws_secret_access_key=key[1],
            )
            self._client_creds = key
        return self._client

    def _check_credentials(self) -> Optional[dict]:
        """Return an error dict if AWS credentials are absent, else None."""
        if not os.environ.get('AWS_ACCESS_KEY_ID') or not os.environ.get('AWS_SECRET_ACCESS_KEY'):
            return {"success": False, "error": "AWS credentials not configured"}
        return None

    # ── ETag cache ────────────────────────────────────────────────────────────

    @property
    def _etag_cache_path(self) -> Path:
        return self._data_dir / ".s3_etag_cache.json"

    def _load_etag_cache(self) -> dict:
        if self._etag_cache_path.exists():
            try:
                return json.loads(self._etag_cache_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_etag_cache(self, cache: dict) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._etag_cache_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            tmp.replace(self._etag_cache_path)
        except Exception as e:
            print(f"[WARN] Could not save ETag cache: {e}", file=sys.stderr)

    # ── user_id lookup ────────────────────────────────────────────────────────

    def lookup_user_id(self, username: str, project: str = None) -> str:
        """Resolve the numeric user_id for an (username, project) pair.

        Results are cached so we only scan the bucket once per (username, project)
        combination per process lifetime.
        """
        cache_key: Tuple[str, str] = (username, project or "")
        if cache_key in self._user_id_cache:
            return self._user_id_cache[cache_key]

        uid = "10"  # safe fallback
        try:
            s3_client = self.get_client()
            search_pattern = f"{username}/{project}/" if project else f"{username}/"
            paginator = s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=self._bucket, Prefix="users/"):
                for obj in page.get('Contents', []):
                    s3_key = obj['Key']
                    if search_pattern in s3_key:
                        parts = s3_key.split('/')
                        if len(parts) >= 2 and parts[0] == 'users':
                            uid = parts[1]
                            print(f"[INFO] Resolved user_id={uid} for {username}",
                                  file=sys.stderr)
                            break
                else:
                    continue
                break
        except Exception as e:
            print(f"[WARN] lookup_user_id failed: {e}", file=sys.stderr)

        self._user_id_cache[cache_key] = uid
        if uid == "10":
            print(
                f"[WARN] Could not resolve user_id for {username} — using fallback '10'",
                file=sys.stderr,
            )
        return uid

    # ── Sync operations ───────────────────────────────────────────────────────

    def sync_all(
        self,
        username: str = None,
        project: str = None,
        clean_first: bool = False,
    ) -> dict:
        """Download all changed tool blocks from S3 then reload the registry.

        Skips files whose S3 ETag matches the locally cached value so only
        genuinely modified blocks are transferred.  Blocking — callers in async
        contexts must use run_in_executor.
        """
        if not S3_AVAILABLE:
            return {"success": False, "error": "boto3 not installed - S3 sync not available"}

        cred_err = self._check_credentials()
        if cred_err:
            return cred_err

        try:
            s3_client = self.get_client()

            paginator = s3_client.get_paginator('list_objects_v2')
            all_objects: list = []
            for page in paginator.paginate(Bucket=self._bucket, Prefix="users/"):
                all_objects.extend(page.get('Contents', []))

            if not all_objects:
                return {"success": True, "message": "No files found in S3", "files_synced": 0}

            self._data_dir.mkdir(exist_ok=True)

            import shutil
            if clean_first:
                if username and project:
                    target = self._data_dir / username / project
                elif username:
                    target = self._data_dir / username
                else:
                    target = None
                if target and target.exists():
                    try:
                        shutil.rmtree(target)
                        print(f"[INFO] ✓ Cleaned {target}")
                    except Exception as e:
                        print(f"[WARN] Clean failed: {e}")

            files_synced = 0
            files_skipped = 0
            s3_files: set = set()

            with self._etag_lock:
                etag_cache = self._load_etag_cache()

                for obj in all_objects:
                    s3_key = obj['Key']
                    if s3_key.endswith('/'):
                        continue
                    if username and username not in s3_key:
                        continue
                    if project and project not in s3_key:
                        continue
                    if '/tools/' not in s3_key:
                        continue

                    parts = s3_key.split('/')
                    local_key = (
                        '/'.join(parts[2:])
                        if len(parts) >= 3 and parts[0] == 'users'
                        else s3_key
                    )
                    s3_files.add(local_key)

                    local_path = self._data_dir / local_key
                    s3_etag = obj.get('ETag', '').strip('"')

                    if etag_cache.get(s3_key) == s3_etag and local_path.exists():
                        files_skipped += 1
                        continue

                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        s3_client.download_file(self._bucket, s3_key, str(local_path))
                        etag_cache[s3_key] = s3_etag
                        files_synced += 1
                        print(f"[INFO] ✓ Downloaded (changed): {s3_key}", file=sys.stderr)
                    except Exception as e:
                        print(f"[ERROR] Download failed ({s3_key}): {e}", file=sys.stderr)

                # Delete orphaned local files (exist locally but not in S3)
                files_deleted = 0
                dirs_deleted = 0
                tools_paths: list = []
                if username and project:
                    tools_paths = [self._data_dir / username / project / "tools"]
                elif username:
                    ud = self._data_dir / username
                    if ud.exists():
                        tools_paths = [p / "tools" for p in ud.iterdir() if p.is_dir()]
                elif self._data_dir.exists():
                    for ud in self._data_dir.iterdir():
                        if ud.is_dir() and not ud.name.startswith('.'):
                            for pd in ud.iterdir():
                                if pd.is_dir() and not pd.name.startswith('.'):
                                    tp = pd / "tools"
                                    if tp.exists():
                                        tools_paths.append(tp)

                for tp in tools_paths:
                    if not tp.exists():
                        continue
                    for lf in tp.rglob("*"):
                        if not lf.is_file():
                            continue
                        try:
                            lf_key = lf.relative_to(self._data_dir).as_posix()
                            if lf_key not in s3_files:
                                lf.unlink()
                                files_deleted += 1
                                print(f"[INFO] Deleted orphan: {lf}", file=sys.stderr)
                        except Exception as e:
                            print(f"[ERROR] Delete check failed ({lf}): {e}", file=sys.stderr)
                    for ld in sorted(tp.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                        if ld.is_dir():
                            try:
                                if not any(ld.iterdir()):
                                    ld.rmdir()
                                    dirs_deleted += 1
                            except Exception:
                                pass

                orphaned_keys = [
                    k for k in list(etag_cache)
                    if k.startswith("users/") and '/'.join(k.split('/')[2:]) not in s3_files
                ]
                for k in orphaned_keys:
                    del etag_cache[k]

                self._save_etag_cache(etag_cache)

            print(
                f"[INFO] Sync complete: {files_synced} downloaded, "
                f"{files_skipped} unchanged, {files_deleted} deleted",
                file=sys.stderr,
            )

            tools_loaded = 0
            if files_synced or files_deleted:
                try:
                    reload_result = self._bridge.reload_tools()
                    if reload_result.get("success"):
                        tools_loaded = reload_result.get("counts", {}).get("tools", 0)
                except Exception as e:
                    print(f"[ERROR] Reload after sync failed: {e}", file=sys.stderr)
                    traceback.print_exc()

            return {
                "success": True,
                "message": (
                    f"Synced {files_synced} files, {files_skipped} unchanged, "
                    f"deleted {files_deleted} orphans, {tools_loaded} tools loaded"
                ),
                "files_synced": files_synced,
                "files_skipped": files_skipped,
                "files_deleted": files_deleted,
                "dirs_deleted": dirs_deleted,
                "tools_loaded": tools_loaded,
                "data_directory": str(self._data_dir),
            }

        except Exception as e:
            print(f"[ERROR] S3 sync error: {e}", file=sys.stderr)
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    def sync_tool(
        self,
        username: str,
        project: str,
        tool_name: str,
        category: str = None,
        user_id: str = None,
    ) -> dict:
        """Download a single changed tool block from S3 then reload the registry.

        Uses a tight S3 prefix scoped to the one tool so only its files are
        listed.  Skips the download when the ETag is unchanged.  Blocking —
        callers must use run_in_executor.
        """
        if not S3_AVAILABLE:
            return {"success": False, "error": "boto3 not installed - S3 sync not available"}

        cred_err = self._check_credentials()
        if cred_err:
            return cred_err

        try:
            s3_client = self.get_client()

            if user_id:
                self._user_id_cache[(username, project or "")] = user_id
            else:
                user_id = self.lookup_user_id(username, project)

            prefix = (
                f"users/{user_id}/{username}/{project}/tools/{category}/{tool_name}/"
                if category
                else f"users/{user_id}/{username}/{project}/tools/{tool_name}/"
            )

            print(f"[INFO] Checking tool '{tool_name}' at S3 prefix: {prefix}", file=sys.stderr)

            self._data_dir.mkdir(exist_ok=True)

            files_synced = 0
            files_skipped = 0
            paginator = s3_client.get_paginator('list_objects_v2')

            with self._etag_lock:
                etag_cache = self._load_etag_cache()

                for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                    for obj in page.get('Contents', []):
                        s3_key = obj['Key']
                        if s3_key.endswith('/'):
                            continue
                        parts = s3_key.split('/', 2)
                        local_key = parts[2] if len(parts) == 3 else s3_key
                        local_path = self._data_dir / local_key
                        s3_etag = obj.get('ETag', '').strip('"')

                        if etag_cache.get(s3_key) == s3_etag and local_path.exists():
                            files_skipped += 1
                            continue

                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            s3_client.download_file(self._bucket, s3_key, str(local_path))
                            etag_cache[s3_key] = s3_etag
                            files_synced += 1
                            print(f"[INFO] ✓ Downloaded (changed): {s3_key}", file=sys.stderr)
                        except Exception as e:
                            print(f"[ERROR] Download failed ({s3_key}): {e}", file=sys.stderr)

                self._save_etag_cache(etag_cache)

            print(
                f"[INFO] Single-tool sync '{tool_name}': "
                f"{files_synced} downloaded, {files_skipped} unchanged",
                file=sys.stderr,
            )

            tools_loaded = 0
            if files_synced:
                try:
                    reload_result = self._bridge.reload_tools()
                    if reload_result.get("success"):
                        tools_loaded = reload_result.get("counts", {}).get("tools", 0)
                except Exception as e:
                    print(f"[ERROR] Reload after single-tool sync failed: {e}", file=sys.stderr)

            return {
                "success": True,
                "files_synced": files_synced,
                "files_skipped": files_skipped,
                "tools_loaded": tools_loaded,
                "tool_name": tool_name,
            }

        except Exception as e:
            print(f"[ERROR] Single-tool sync error: {e}", file=sys.stderr)
            traceback.print_exc()
            return {"success": False, "error": str(e)}


# ── Module-level singleton ────────────────────────────────────────────────────

block_downloader = BlockDownloader(
    bucket=os.environ.get('S3_BUCKET_NAME', 'grafux-user-files'),
    data_dir=_get_project_root() / "data",
    bridge=bridge,
)
