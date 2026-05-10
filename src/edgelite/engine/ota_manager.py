"""OTA升级管理器 - v1.1 Pro版特性"""

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

logger = logging.getLogger(__name__)


class OTAManager:
    """OTA升级管理器，支持远程升级和回滚"""

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=60.0)
        self._upgrade_dir = Path("data/ota")
        self._backup_dir = Path("data/ota/backups")
        self._current_version = self._get_current_version()
        self._lock = asyncio.Lock()

    def _get_current_version(self) -> str:
        """获取当前版本"""
        try:
            from edgelite import __version__

            return __version__
        except Exception:
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
                return resp.json()
            return None
        except Exception as e:
            logger.error("检查更新失败: %s", e)
            return None

    async def download_update(self, version: str, download_url: str) -> Path | None:
        """下载更新包"""
        self._upgrade_dir.mkdir(parents=True, exist_ok=True)
        temp_file = self._upgrade_dir / f"update-{version}.zip"

        try:
            logger.info("开始下载更新包: %s", download_url)
            async with self._client.stream("GET", download_url) as response:
                if response.status_code != 200:
                    raise Exception(f"下载失败: HTTP {response.status_code}")

                total_size = int(response.headers.get("content-length", 0))
                downloaded = 0

                with open(temp_file, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size:
                            progress = downloaded / total_size * 100
                            logger.debug("下载进度: %.1f%%", progress)

            logger.info("更新包下载完成: %s", temp_file)
            return temp_file
        except Exception as e:
            logger.error("下载更新包失败: %s", e)
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
                logger.info("更新包验证通过")
                return True
            else:
                logger.error("更新包验证失败: SHA256不匹配")
                return False
        except Exception as e:
            logger.error("验证更新包失败: %s", e)
            return False

    async def backup_current(self) -> Path | None:
        """备份当前版本"""
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self._backup_dir / f"backup-{self._current_version}-{timestamp}"

        try:
            # 备份关键文件
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

            logger.info("当前版本备份完成: %s", backup_path)
            return backup_path
        except Exception as e:
            logger.error("备份失败: %s", e)
            return None

    async def apply_update(self, update_file: Path, expected_sha256: str | None = None) -> bool:
        """应用更新"""
        async with self._lock:
            try:
                if not update_file.exists():
                    logger.error("更新文件不存在: %s", update_file)
                    return False

                if expected_sha256:
                    is_valid = await self.verify_update(update_file, expected_sha256)
                    if not is_valid:
                        logger.error("SHA256校验失败，更新包可能被篡改")
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
                            if member.startswith("/") or ".." in member:
                                logger.error("压缩包包含不安全路径: %s", member)
                                return False
                        await asyncio.to_thread(zf.extractall, str(extract_dir))

                    src_dir = extract_dir
                    if (extract_dir / "edgelite").is_dir():
                        src_dir = extract_dir / "edgelite"

                    app_dir = Path(__file__).resolve().parent.parent
                    if not (app_dir / "__init__.py").exists():
                        logger.error("无法确定应用目录: %s", app_dir)
                        return False

                    for item in src_dir.rglob("*"):
                        if item.is_file():
                            rel = item.relative_to(src_dir)
                            dest = app_dir / rel
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(str(item), str(dest))

                    logger.info("更新应用成功，文件已替换")
                    return True
                finally:
                    shutil.rmtree(str(extract_dir), ignore_errors=True)

            except Exception as e:
                logger.error("应用更新失败: %s", e)
                return False

    async def rollback(self, backup_version: str | None = None) -> bool:
        """回滚到指定版本"""
        async with self._lock:
            try:
                # 查找备份
                if backup_version:
                    backups = list(self._backup_dir.glob(f"backup-{backup_version}-*"))
                else:
                    backups = sorted(self._backup_dir.glob("backup-*"), reverse=True)

                if not backups:
                    logger.error("未找到可用的备份")
                    return False

                backup_path = backups[0]
                logger.info("开始回滚到: %s", backup_path)

                # 恢复文件
                for item in ["src", "configs", "requirements.txt", "pyproject.toml"]:
                    src = backup_path / item
                    dst = Path(item)
                    if src.exists():
                        if dst.exists():
                            if dst.is_dir():
                                shutil.rmtree(dst)
                            else:
                                dst.unlink()
                        if src.is_dir():
                            await asyncio.to_thread(shutil.copytree, src, dst)
                        else:
                            shutil.copy2(src, dst)

                logger.info("回滚成功")
                return True
            except Exception as e:
                logger.error("回滚失败: %s", e)
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
                    info["path"] = str(backup_path)
                    backups.append(info)
                except Exception as e:
                    logger.debug("读取备份信息失败: %s", e)
        return sorted(backups, key=lambda x: x.get("created_at", ""), reverse=True)

    @property
    def current_version(self) -> str:
        return self._current_version
