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
from edgelite.constants import (  # FIXED: 原问题-魔法数字timeout=60.0
    _OTA_DOWNLOAD_CHUNK,
    _OTA_DOWNLOAD_TIMEOUT,
)

logger = logging.getLogger(__name__)


class OTAManager:
    """OTA upgrade manager with remote upgrade and rollback support"""

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=_OTA_DOWNLOAD_TIMEOUT)  # FIXED: 原问题-魔法数字timeout=60.0
        self._upgrade_dir = Path("data/ota")
        self._backup_dir = Path("data/ota/backups")
        self._current_version = self._get_current_version()
        self._lock = asyncio.Lock()
        # FIXED(安全-P1): 下载与应用分别持锁——并发下载同一版本会交错写同一个 .part 文件
        self._download_lock = asyncio.Lock()

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
        """下载更新包（支持断点续传：失败保留 .part，重试时从已下载字节处继续）"""
        self._upgrade_dir.mkdir(parents=True, exist_ok=True)
        temp_file = self._upgrade_dir / f"update-{version}.zip"
        partial_file = self._upgrade_dir / f"update-{version}.zip.part"

        # FIXED(安全-P1): 下载持专用锁，防止并发下载交错写同一 .part 文件产生损坏包
        async with self._download_lock:
            try:
                # FIXED(P2): 断点续传——已有部分文件时尝试 Range 续传；服务端不支持则重头
                resume_from = 0
                if partial_file.exists():
                    try:
                        resume_from = partial_file.stat().st_size
                    except OSError:
                        resume_from = 0
                headers: dict[str, str] = {}
                if resume_from > 0:
                    headers["Range"] = f"bytes={resume_from}-"
                    logger.info("Resuming download from byte %d: %s", resume_from, download_url)

                logger.info("Downloading update package: %s", download_url)
                async with self._client.stream("GET", download_url, headers=headers) as response:
                    # FIXED: 非 200/206（404/500 等）必须显式报错——原重构中丢失该分支，
                    # 会把错误响应体当作更新包写入
                    if response.status_code not in (200, 206):
                        raise Exception(f"Download failed: HTTP {response.status_code}")
                    # 206 = 服务端支持续传；200 = 不支持（含 Range 头仍返回全量），需重头写
                    if response.status_code == 206 and resume_from > 0:
                        write_mode = "ab"
                        downloaded = resume_from
                    else:
                        write_mode = "wb"
                        downloaded = 0

                    # FIXED: P3-1 content-length may be non-numeric
                    try:
                        total_size = int(response.headers.get("content-length", 0))
                        if response.status_code == 206 and resume_from > 0:
                            total_size += resume_from
                    except (ValueError, TypeError):
                        total_size = 0

                    # FIXED: 原问题-下载写入无异常保护，部分写入后崩溃导致文件损坏
                    # 现先写入 .part 文件且失败时保留（供下次续传），完成后才 replace 为目标文件
                    try:
                        with open(partial_file, write_mode) as f:
                            async for chunk in response.aiter_bytes(
                                chunk_size=_OTA_DOWNLOAD_CHUNK
                            ):  # FIXED: 原问题-chunk_size=8192魔法数字
                                f.write(chunk)
                                downloaded += len(chunk)
                                if total_size:
                                    progress = downloaded / total_size * 100
                                    logger.debug("Download progress: %.1f%%", progress)
                        # FIXED(P2): Path.replace 在 Windows/POSIX 上都是原子覆盖，rename 在
                        # Windows 目标存在时会抛 FileExistsError
                        partial_file.replace(temp_file)
                    except Exception:
                        # 保留 .part 供断点续传；仅在写入模式为重头且文件损坏风险高时清理由下次决定
                        raise

                logger.info("Update package downloaded: %s", temp_file)
                return temp_file
            except Exception as e:
                logger.error("Download update package failed: %s", e)
                # FIXED(P2): 失败时不再误删已完成的历史包 temp_file，.part 保留供续传
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
                else:
                    # FIXED(安全-P1): 无完整性校验直接覆盖应用目录是危险路径，至少显式告警
                    logger.warning(
                        "apply_update called without expected_sha256 - "
                        "integrity verification skipped (caller should fail-closed instead)"
                    )

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

                        # FIXED(安全-P1): 逐文件覆盖前备份原文件，任一复制失败（磁盘满/权限）
                        # 时恢复全部已覆盖文件，避免应用目录处于半新半旧导致下次无法启动
                        overwrite_backup_dir = Path(tempfile.mkdtemp(prefix="edgelite_overwrite_bak_"))
                        applied_files: list[tuple[Path, Path | None]] = []  # (dest, 备份路径或 None=新增文件)
                        try:
                            for item in staging_dir.rglob("*"):
                                if item.is_file():
                                    rel = item.relative_to(staging_dir)
                                    dest = app_dir / rel
                                    dest.parent.mkdir(parents=True, exist_ok=True)
                                    backup_file: Path | None = None
                                    if dest.exists():
                                        backup_file = overwrite_backup_dir / rel
                                        backup_file.parent.mkdir(parents=True, exist_ok=True)
                                        shutil.copy2(str(dest), str(backup_file))
                                    shutil.copy2(str(item), str(dest))
                                    applied_files.append((dest, backup_file))
                        except Exception:
                            # 逆序恢复：被覆盖的还原备份，新增的删除
                            for dest, backup_file in reversed(applied_files):
                                try:
                                    if backup_file is not None and backup_file.exists():
                                        shutil.copy2(str(backup_file), str(dest))
                                    elif backup_file is None and dest.exists():
                                        dest.unlink()
                                except Exception as restore_err:
                                    logger.error(
                                        "Overwrite rollback failed for %s: %s (backup: %s)",
                                        dest,
                                        restore_err,
                                        backup_file,
                                    )
                            raise
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
                # FIXED(安全-P1): 不再先 rmtree 再复制——复制中途失败会导致代码目录丢失且
                # 无自动恢复。改为先把现有目录改名挪走（原子），复制成功再清理，失败则挪回
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
                            # 现有目标先挪到 trash（rename 原子操作），不直接删除
                            trash_path: Path | None = None
                            if dst.exists():
                                trash_path = staging / "_replaced_existing" / item
                                trash_path.parent.mkdir(parents=True, exist_ok=True)
                                dst.rename(trash_path)
                            try:
                                if (staging / item).is_dir():
                                    await asyncio.to_thread(shutil.copytree, staging / item, dst)
                                else:
                                    shutil.copy2(str(staging / item), str(dst))
                            except Exception:
                                # 恢复被挪走的现有目录，保持回滚前的状态
                                if trash_path is not None and trash_path.exists():
                                    if dst.exists():
                                        if dst.is_dir():
                                            shutil.rmtree(dst, ignore_errors=True)
                                        else:
                                            dst.unlink()
                                    trash_path.rename(dst)
                                raise
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

    async def close(self) -> None:
        """释放 HTTP 客户端连接池（进程退出/服务停用时调用，防止资源泄漏）"""
        try:
            await self._client.aclose()
        except Exception as e:
            logger.debug("OTA httpx client close failed: %s", e)
