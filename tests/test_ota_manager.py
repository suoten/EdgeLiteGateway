"""OTA 升级管理器测试 - 版本检测/SHA256校验/下载/备份

覆盖 engine/ota_manager.py：
- OTAManager: _get_current_version / verify_update (SHA256) / check_update / download_update / backup_current
- verify_update: 匹配/不匹配/文件不存在
- check_update: 未配置 URL / HTTP 异常
- download_update: HTTP 错误返回 None
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from edgelite.engine.ota_manager import OTAManager


@pytest.fixture
def ota_manager():
    """创建 OTAManager 实例（httpx.AsyncClient 在构造时创建，不发起连接）"""
    return OTAManager()


class TestGetCurrentVersion:
    def test_returns_version_string(self, ota_manager):
        """_get_current_version 应返回 edgelite.__version__"""
        version = ota_manager._current_version
        # __version__ = "1.0.0"
        assert version == "1.0.0"


class TestVerifyUpdate:
    @pytest.mark.asyncio
    async def test_verify_success(self, ota_manager, tmp_path):
        """SHA256 匹配时返回 True"""
        content = b"update package content"
        pkg = tmp_path / "update.zip"
        pkg.write_bytes(content)
        expected_sha256 = hashlib.sha256(content).hexdigest()

        result = await ota_manager.verify_update(pkg, expected_sha256)
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_mismatch(self, ota_manager, tmp_path):
        """SHA256 不匹配时返回 False"""
        content = b"update package content"
        pkg = tmp_path / "update.zip"
        pkg.write_bytes(content)
        wrong_sha256 = "0" * 64  # 错误的哈希

        result = await ota_manager.verify_update(pkg, wrong_sha256)
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_nonexistent_file(self, ota_manager, tmp_path):
        """文件不存在时返回 False"""
        pkg = tmp_path / "nonexistent.zip"
        result = await ota_manager.verify_update(pkg, "any_hash")
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_empty_file(self, ota_manager, tmp_path):
        """空文件的 SHA256 应匹配已知值"""
        pkg = tmp_path / "empty.zip"
        pkg.write_bytes(b"")
        # 空字符串的 SHA256
        expected_sha256 = hashlib.sha256(b"").hexdigest()

        result = await ota_manager.verify_update(pkg, expected_sha256)
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_large_file_chunked(self, ota_manager, tmp_path):
        """大文件分块读取验证"""
        # 创建大于 8192 字节的文件以测试分块读取
        content = b"\x42" * 20000
        pkg = tmp_path / "large.zip"
        pkg.write_bytes(content)
        expected_sha256 = hashlib.sha256(content).hexdigest()

        result = await ota_manager.verify_update(pkg, expected_sha256)
        assert result is True


class TestCheckUpdate:
    @pytest.mark.asyncio
    async def test_no_url_configured(self, ota_manager):
        """未配置 OTA URL 时返回 None"""
        mock_config = MagicMock()
        mock_config.ota_update_url = ""
        with patch("edgelite.engine.ota_manager.get_config", return_value=mock_config):
            result = await ota_manager.check_update()
        assert result is None

    @pytest.mark.asyncio
    async def test_none_url_configured(self, ota_manager):
        """OTA URL 为 None 时返回 None"""
        mock_config = MagicMock()
        mock_config.ota_update_url = None
        with patch("edgelite.engine.ota_manager.get_config", return_value=mock_config):
            result = await ota_manager.check_update()
        assert result is None

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self, ota_manager):
        """HTTP 请求异常时返回 None"""
        mock_config = MagicMock()
        mock_config.ota_update_url = "http://ota.example.com"

        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch("edgelite.engine.ota_manager.get_config", return_value=mock_config),
            patch.object(ota_manager, "_client", mock_client),
        ):
            result = await ota_manager.check_update()
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_check(self, ota_manager):
        """HTTP 200 返回更新信息"""
        mock_config = MagicMock()
        mock_config.ota_update_url = "http://ota.example.com"

        update_info = {"version": "1.1.0", "download_url": "http://ota.example.com/pkg.zip"}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value=update_info)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch("edgelite.engine.ota_manager.get_config", return_value=mock_config),
            patch.object(ota_manager, "_client", mock_client),
        ):
            result = await ota_manager.check_update()
        assert result == update_info

    @pytest.mark.asyncio
    async def test_json_parse_error_returns_none(self, ota_manager):
        """响应非 JSON 时返回 None"""
        mock_config = MagicMock()
        mock_config.ota_update_url = "http://ota.example.com"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(side_effect=ValueError("not JSON"))

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch("edgelite.engine.ota_manager.get_config", return_value=mock_config),
            patch.object(ota_manager, "_client", mock_client),
        ):
            result = await ota_manager.check_update()
        assert result is None


class TestDownloadUpdate:
    @pytest.mark.asyncio
    async def test_http_error_returns_none(self, ota_manager):
        """HTTP 非 200 时返回 None"""
        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_stream_ctx)

        with patch.object(ota_manager, "_client", mock_client):
            result = await ota_manager.download_update("1.1.0", "http://ota.example.com/pkg.zip")
        assert result is None


class TestBackupCurrent:
    @pytest.mark.asyncio
    async def test_backup_creates_dir_and_info(self, ota_manager, tmp_path, monkeypatch):
        """备份应创建目录和 backup_info.json"""
        backup_dir = tmp_path / "backups"
        monkeypatch.setattr(ota_manager, "_backup_dir", backup_dir)

        result = await ota_manager.backup_current()
        assert result is not None
        assert result.is_dir()
        assert (result / "backup_info.json").exists()

        info = json.loads((result / "backup_info.json").read_text())
        assert info["version"] == ota_manager._current_version
        assert "timestamp" in info


class TestApplyUpdate:
    @pytest.mark.asyncio
    async def test_nonexistent_file_returns_false(self, ota_manager, tmp_path):
        """更新文件不存在时返回 False"""
        result = await ota_manager.apply_update(tmp_path / "nonexistent.zip")
        assert result is False

    @pytest.mark.asyncio
    async def test_sha256_verification_failure(self, ota_manager, tmp_path):
        """SHA256 验证失败时返回 False"""
        pkg = tmp_path / "update.zip"
        pkg.write_bytes(b"content")
        result = await ota_manager.apply_update(pkg, expected_sha256="0" * 64)
        assert result is False
