"""固件签名 API 安全回归测试（审计 P2 修复：firmware_path 白名单）

覆盖 src/edgelite/api/firmware_signature.py：
- _validate_firmware_path 拒绝绝对路径/盘符/UNC/父目录引用；
- resolve() 后必须位于白名单目录（data/ota + EDGELITE_OTA_DIR）内；
- 白名单外路径一律 404，不区分存在性（防差分文件探测 oracle）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "src")

import pytest
from fastapi import HTTPException

from edgelite.api.firmware_signature import _validate_firmware_path


@pytest.fixture
def ota_dir(tmp_path, monkeypatch):
    """构造临时 data/ota 白名单目录并切换 CWD"""
    base = tmp_path / "data" / "ota"
    base.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return base


class TestFirmwarePathWhitelist:
    def test_relative_path_inside_ota_dir_allowed(self, ota_dir):
        target = ota_dir / "fw-v1.bin"
        target.write_bytes(b"firmware")
        resolved = _validate_firmware_path("data/ota/fw-v1.bin")
        assert resolved == target.resolve()

    def test_nested_path_inside_ota_dir_allowed(self, ota_dir):
        sub = ota_dir / "sub"
        sub.mkdir()
        target = sub / "fw.bin"
        target.write_bytes(b"x")
        resolved = _validate_firmware_path("data/ota/sub/fw.bin")
        assert resolved == target.resolve()

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/shadow",  # 绝对路径
            "C:\\Windows\\system32\\config\\SAM",  # 盘符
            "../.master_key",  # 父目录引用
            "data/ota/../../.env",  # 白名单内出发的遍历
            "\\\\server\\share\\fw.bin",  # UNC
            "data/secrets.txt",  # 白名单目录外
        ],
    )
    def test_dangerous_paths_rejected(self, ota_dir, path):
        """P2 回归: 任意路径探测（/etc/shadow、data/.master_key 等）必须被拒绝。"""
        with pytest.raises(HTTPException) as e:
            _validate_firmware_path(path)
        assert e.value.status_code in (400, 404)

    def test_outside_whitelist_returns_404_uniformly(self, ota_dir, tmp_path):
        """白名单外路径统一 404——即使文件真实存在也不暴露其存在性。"""
        existing = tmp_path / "secret.txt"
        existing.write_bytes(b"top secret")
        missing = tmp_path / "no-such-file.txt"
        for p in (existing, missing):
            with pytest.raises(HTTPException) as e:
                _validate_firmware_path(str(p.relative_to(tmp_path)))
            assert e.value.status_code == 404

    def test_empty_path_rejected(self, ota_dir):
        with pytest.raises(HTTPException):
            _validate_firmware_path("")

    def test_custom_ota_dir_env_allows_access(self, ota_dir, monkeypatch, tmp_path):
        """EDGELITE_OTA_DIR 环境变量可追加自定义固件目录（不新增配置键）。"""
        custom = tmp_path / "custom_fw"
        custom.mkdir()
        target = custom / "fw.bin"
        target.write_bytes(b"x")
        monkeypatch.setenv("EDGELITE_OTA_DIR", str(custom))
        resolved = _validate_firmware_path(str(target))
        assert resolved == target.resolve()

    def test_traversal_via_symlink_style_dotdot_rejected(self, ota_dir):
        with pytest.raises(HTTPException):
            _validate_firmware_path("data/ota/../../../etc/passwd")

    def test_windows_drive_relative_form_rejected(self, ota_dir):
        """盘符相对形式（C:fw.bin）—— is_absolute 为 False 但含盘符，必须拒绝。"""
        with pytest.raises(HTTPException) as e:
            _validate_firmware_path("C:fw.bin")
        assert e.value.status_code == 400


class TestPathIsRelativeToFirmwareBase:
    def test_resolve_anchors_to_cwd(self, ota_dir):
        """白名单根目录应以 CWD 解析（与 engine/ota_manager 的 data/ota 一致）。"""
        assert Path("data/ota").resolve() == ota_dir.resolve()
