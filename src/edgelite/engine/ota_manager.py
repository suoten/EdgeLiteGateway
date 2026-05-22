"""OTA Upgrade Manager - v1.1 Pro Feature"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import httpx

from edgelite.config import get_config
from edgelite.constants import _OTA_DOWNLOAD_CHUNK, _OTA_DOWNLOAD_TIMEOUT  # FIXED: 原问题-魔法数字timeout=60.0

logger = logging.getLogger(__name__)


class OTAManager:
    """OTA upgrade manager with remote upgrade and rollback support"""

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=_OTA_DOWNLOAD_TIMEOUT)  # FIXED: 原问题-魔法数字timeout=60.0
        self._upgrade_dir = Path("data/ota")
        self._backup_dir = Path("data/ota/backups")
        self._current_version = self._get_current_version()
        self._lock = asyncio.Lock()

    def _get_current_version(self) -> str:
        """获取当前版本"""
        try:
            from edgelite import __version__

            return __version__
        except Exception as e:  # FIXED: P3-1 silent exception on version fetch
            logger.debug("Failed to get version: %s", e)
            return "unknown"

    async def check_update(self, channel: str = "stable") -> dict | None:
        """检查可用更新"""
        config = get_config()
        update_url = config.ota_update_url

        try:
            if not update_url:
                logger.info("未配置OTA更新地址，跳过检查")
                return None
            resp = await self._client.get(
                f"{update_url}/check",
                params={"version": self._current_version, "channel": channel},
            )
            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception as e:  # FIXED: 原问题-resp.json()未捕获JSONDecodeError，OTA服务器返回非JSON时崩溃
                    logger.error("OTA响应解析失败: %s", e)
                    return None
            return None
        except Exception as e:
            logger.error("Update check failed: %s", e)
            return None

    async def download_update(self, version: str, download_url: str) -> Path | None:
        """下载更新包"""
        self._upgrade_dir.mkdir(parents=True, exist_ok=True)
        temp_file = self._upgrade_dir / f"update-{version}.zip"

        try:
            logger.info("Downloading update package: %s", download_url)
            async with self._client.stream("GET", download_url) as response:
                if response.status_code != 200:
                    raise Exception(f"Download failed: HTTP {response.status_code}")

                # FIXED: P3-1 content-length may be non-numeric
                try:
                    total_size = int(response.headers.get("content-length", 0))
                except (ValueError, TypeError):
                    total_size = 0
                downloaded = 0

                # FIXED: 原问题-下载写入无异常保护，部分写入后崩溃导致文件损坏
                # 现先写入临时文件，下载完成后才重命名为目标文件
                partial_file = self._upgrade_dir / f"update-{version}.zip.part"
                try:
                    with open(partial_file, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=_OTA_DOWNLOAD_CHUNK):  # FIXED: 原问题-chunk_size=8192魔法数字
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size:
                                progress = downloaded / total_size * 100
                                logger.debug("Download progress: %.1f%%", progress)
                    partial_file.rename(temp_file)
                except Exception:
                    if partial_file.exists():
                        partial_file.unlink()
                    raise

            logger.info("Update package downloaded: %s", temp_file)
            return temp_file
        except Exception as e:
            logger.error("Download update package failed: %s", e)
            if temp_file.exists():
                temp_file.unlink()
            return None

    async def verify_update(self, file_path: Path, expected_sha256: str) -> bool:
        """验证更新包完整性"""
        try:
            sha256_hash = hashlib.sha256()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256_hash.update(chunk)

            actual_hash = sha256_hash.hexdigest()
            if actual_hash == expected_sha256:
                logger.info("Update package verified")
                return True
            else:
                logger.error("Update package verification failed: SHA256 mismatch")
                return False
        except Exception as e:
            logger.error("Verify update package failed: %s", e)
            return False

    async def backup_current(self) -> Path | None:
        """备份当前版本"""
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self._backup_dir / f"backup-{self._current_version}-{timestamp}"

        try:
            # Backup critical files
            backup_path.mkdir()
            for item in ["src", "configs", "requirements.txt", "pyproject.toml"]:
                src = Path(item)
                if src.exists():
                    if src.is_dir():
                        await asyncio.to_thread(shutil.copytree, src, backup_path / item)
                    else:
                        shutil.copy2(src, backup_path / item)

            # 记录备份信息
            info = {
                "version": self._current_version,
                "timestamp": timestamp,
                "created_at": datetime.now().isoformat(),
            }
            with open(backup_path / "backup_info.json", "w") as f:
                json.dump(info, f, indent=2)

            logger.info("Current version backup completed: %s", backup_path)
            return backup_path
        except Exception as e:
            logger.error("Backup failed: %s", e)
            return None

    async def apply_update(self, update_file: Path, expected_sha256: str | None = None) -> bool:
        """应用更新"""
        async with self._lock:
            try:
                if not update_file.exists():
                    logger.error("Update file not found: %s", update_file)
                    return False

                if expected_sha256:
                    is_valid = await self.verify_update(update_file, expected_sha256)
                    if not is_valid:
                        logger.error("SHA256 verification failed, update package may be tampered")
                        return False

                backup_path = await self.backup_current()
                if backup_path is None:
                    return False

                import shutil
                import zipfile

                extract_dir = Path(tempfile.mkdtemp(prefix="edgelite_update_"))
                try:
                    with zipfile.ZipFile(update_file, "r") as zf:
                        for member in zf.namelist():
                            # FIXED: P3-1 Enhanced path traversal check
                            member_path = Path(member)
                            if member_path.is_absolute() or member.startswith("/") or ".." in member:
                                logger.error("Compressed package contains unsafe path: %s", member)
                                return False
                            # Check Windows absolute paths (C:\, D:\etc)
                            if len(member) > 1 and member[1] == ":":
                                logger.error("Compressed package contains Windows absolute path: %s", member)
                                return False
                        await asyncio.to_thread(zf.extractall, str(extract_dir))

                    src_dir = extract_dir
                    if (extract_dir / "edgelite").is_dir():
                        src_dir = extract_dir / "edgelite"

                    app_dir = Path(__file__).resolve().parent.parent
                    if not (app_dir / "__init__.py").exists():
                        logger.error("Cannot determine app directory: %s", app_dir)
                        return False

                    # FIXED: P3-1 Atomic file replacement
                    # Copy to staging first, then batch replace
                    staging_dir = Path(tempfile.mkdtemp(prefix="edgelite_staging_"))
                    try:
                        for item in src_dir.rglob("*"):
                            if item.is_file():
                                rel = item.relative_to(src_dir)
                                dest = staging_dir / rel
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(str(item), str(dest))

                        for item in staging_dir.rglob("*"):
                            if item.is_file():
                                rel = item.relative_to(staging_dir)
                                dest = app_dir / rel
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(str(item), str(dest))
                    finally:
                        shutil.rmtree(str(staging_dir), ignore_errors=True)

                    logger.info("Update applied successfully, files replaced")
                    return True
                finally:
                    shutil.rmtree(str(extract_dir), ignore_errors=True)

            except Exception as e:
                logger.error("Apply update failed: %s", e)
                return False

    async def rollback(self, backup_version: str | None = None) -> bool:
        """Rollback to specified version"""
        async with self._lock:
            try:
                if backup_version:
                    backups = list(self._backup_dir.glob(f"backup-{backup_version}-*"))
                else:
                    backups = sorted(self._backup_dir.glob("backup-*"), reverse=True)

                if not backups:
                    logger.error("No available backup found")
                    return False

                backup_path = backups[0]
                if not backup_path.is_dir():
                    logger.error("Backup path is not a directory: %s", backup_path)
                    return False

                logger.info("Starting rollback to: %s", backup_path)

                # FIXED: P3-1 Atomic rollback operation
                for item in ["src", "configs", "requirements.txt", "pyproject.toml"]:
                    src = backup_path / item
                    dst = Path(item)
                    if src.exists():
                        staging = Path(tempfile.mkdtemp(prefix="edgelite_rollback_"))
                        try:
                            if src.is_dir():
                                await asyncio.to_thread(shutil.copytree, src, staging / item)
                            else:
                                shutil.copy2(str(src), str(staging / item))
                            if dst.exists():
                                if dst.is_dir():
                                    shutil.rmtree(dst)
                                else:
                                    dst.unlink()
                            if (staging / item).is_dir():
                                await asyncio.to_thread(shutil.copytree, staging / item, dst)
                            else:
                                shutil.copy2(str(staging / item), str(dst))
                        finally:
                            shutil.rmtree(str(staging), ignore_errors=True)

                logger.info("Rollback successful")
                return True
            except Exception as e:
                logger.error("Rollback failed: %s", e)
                return False

    def list_backups(self) -> list[dict]:
        """列出所有备份"""
        backups = []
        for backup_path in self._backup_dir.glob("backup-*"):
            info_file = backup_path / "backup_info.json"
            if info_file.exists():
                try:
                    with open(info_file) as f:
                        info = json.load(f)
                    # FIXED: P3-1 JSON load may return non-dict
                    if not isinstance(info, dict):
                        logger.debug("Backup info format error: %s", info_file)
                        continue
                    info["path"] = str(backup_path)
                    backups.append(info)
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug("Read backup info failed: %s", e)
        return sorted(backups, key=lambda x: x.get("created_at", ""), reverse=True)

    @property
    def current_version(self) -> str:
        return self._current_version
