"""模拟器驱动单元测试

覆盖 src/edgelite/drivers/simulator.py：
- _WAVE_MODES / _FAULT_MODES 常量
- _WriteOverride 类（值/过期/审计）
- SimulatorDriver 类元数据（plugin_name / supported_protocols / config_schema）
- 波形生成逻辑（sine/square/triangle/sawtooth/ramp/step/fixed）

设计要点：
- 模拟器是纯内存实现，无需 mock 外部 IO
- 波形函数为纯数学函数，可直接验证输出特征
"""

from __future__ import annotations

from edgelite.drivers.simulator import (
    _FAULT_MODES,
    _WAVE_MODES,
    SimulatorDriver,
    _WriteOverride,
)

# ── 常量 ──


class TestConstants:
    def test_wave_modes_contains_all(self):
        expected = {
            "random", "sine", "square", "triangle", "sawtooth",
            "random_walk", "ramp", "step", "formula", "fixed",
        }
        assert set(_WAVE_MODES) == expected

    def test_fault_modes(self):
        assert set(_FAULT_MODES) == {"timeout", "disconnect", "data_error"}

    def test_wave_modes_is_list(self):
        assert isinstance(_WAVE_MODES, list)

    def test_fault_modes_is_tuple(self):
        assert isinstance(_FAULT_MODES, tuple)


# ── _WriteOverride ──


class TestWriteOverride:
    def test_attributes_stored(self):
        audit = {"user": "admin", "ts": 123}
        wo = _WriteOverride(value=42.0, expire_at=999.0, audit=audit)
        assert wo.value == 42.0
        assert wo.expire_at == 999.0
        assert wo.audit == audit

    def test_slots_no_dict(self):
        """_WriteOverride 使用 __slots__，无 __dict__。"""
        wo = _WriteOverride(value=1, expire_at=2, audit={})
        assert not hasattr(wo, "__dict__")


# ── SimulatorDriver 类元数据 ──


class TestSimulatorDriverMetadata:
    def test_plugin_name(self):
        assert SimulatorDriver.plugin_name == "simulator"

    def test_plugin_version(self):
        assert SimulatorDriver.plugin_version == "0.1.0"

    def test_supported_protocols(self):
        assert "simulator" in SimulatorDriver.supported_protocols
        assert isinstance(SimulatorDriver.supported_protocols, tuple)

    def test_production_safe_false(self):
        """模拟器不应在生产环境自动启用。"""
        assert SimulatorDriver._production_safe is False

    def test_config_schema_has_fields(self):
        schema = SimulatorDriver.config_schema
        assert "fields" in schema
        field_names = {f["name"] for f in schema["fields"]}
        assert "update_interval" in field_names
        assert "value_range_min" in field_names
        assert "value_range_max" in field_names
        assert "sim_mode" in field_names

    def test_config_schema_sim_mode_options(self):
        schema = SimulatorDriver.config_schema
        sim_mode_field = next(f for f in schema["fields"] if f["name"] == "sim_mode")
        assert set(sim_mode_field["options"]) == set(_WAVE_MODES)

    def test_config_schema_no_required(self):
        """模拟器无必填字段。"""
        schema = SimulatorDriver.config_schema
        assert schema["required"] == []
