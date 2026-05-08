"""DL/T 645-2007 驱动单元测试"""

import sys

sys.path.insert(0, "src")

from edgelite.drivers.dlt645 import DLT645_DI_MAP, Dlt645Driver


class TestDlt645Frame:
    """帧编解码测试"""

    def test_add_33h_sub_33h_roundtrip(self):
        """+33H/-33H 互逆"""
        data = bytes([0x11, 0x22, 0x33, 0x44, 0x55])
        driver = Dlt645Driver.__new__(Dlt645Driver)
        assert driver._sub_33h(driver._add_33h(data)) == data

    def test_add_33h_result(self):
        """验证+33H计算结果"""
        driver = Dlt645Driver.__new__(Dlt645Driver)
        data = bytes([0x01, 0x02, 0x33])
        result = driver._add_33h(data)
        assert result == bytes([0x34, 0x35, 0x66])

    def test_calculate_cs(self):
        """帧校验和计算"""
        driver = Dlt645Driver.__new__(Dlt645Driver)
        frame_data = bytes([0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x11, 0x04, 0x33, 0x34, 0x41])
        cs = driver._calculate_cs(frame_data)
        assert 0 <= cs <= 255

    def test_validate_cs_correct(self):
        """CS校验-正确帧"""
        driver = Dlt645Driver.__new__(Dlt645Driver)
        body = bytes([0x68, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x68, 0x11, 0x04, 0x33, 0x34, 0x41])
        cs = driver._calculate_cs(body[1:])
        full = body + bytes([cs, 0x16])
        assert driver._validate_cs(full) is True

    def test_validate_cs_tampered(self):
        """CS校验-篡改帧"""
        driver = Dlt645Driver.__new__(Dlt645Driver)
        body = bytes([0x68, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x68, 0x11, 0x04, 0x33, 0x34, 0x41])
        cs = driver._calculate_cs(body[1:])
        full = body + bytes([(cs + 1) & 0xFF, 0x16])
        assert driver._validate_cs(full) is False

    def test_encode_address(self):
        """表地址BCD编码，低字节在前"""
        driver = Dlt645Driver.__new__(Dlt645Driver)
        result = driver._encode_address("000000000001")
        assert len(result) == 6
        assert result[0] == 0x01

    def test_decode_bcd(self):
        """BCD解析"""
        driver = Dlt645Driver.__new__(Dlt645Driver)
        result = driver._decode_bcd(bytes([0x12, 0x34]), decimal_places=2)
        assert abs(result - 34.12) < 0.01

    def test_decode_float32(self):
        """IEEE 754浮点解析"""
        driver = Dlt645Driver.__new__(Dlt645Driver)
        import struct

        expected = 220.5
        data = struct.pack("<f", expected)
        result = driver._decode_float32(data)
        assert abs(result - expected) < 1e-6

    def test_di_map_completeness(self):
        """预置数据标识映射完整性"""
        required_keys = ["voltage_a", "current_a", "active_power", "energy_pos_active", "frequency"]
        for key in required_keys:
            assert key in DLT645_DI_MAP, f"缺少数据标识: {key}"
            assert "di" in DLT645_DI_MAP[key]
            assert "unit" in DLT645_DI_MAP[key]

    def test_build_read_frame_format(self):
        """读数据帧格式(0x11)"""
        driver = Dlt645Driver.__new__(Dlt645Driver)
        frame = driver._build_read_frame("000000000001", "02010100")
        assert frame[0] == 0x68
        assert frame[7] == 0x68
        assert frame[8] == 0x11
        assert frame[-1] == 0x16
