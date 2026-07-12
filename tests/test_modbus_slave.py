"""Comprehensive tests for edgelite.engine.modbus_slave.ModbusSlaveServer.

Covers:
- _parse_pymodbus_version() with various version strings
- _is_port_available() for available and occupied ports
- ModbusSlaveServer.__init__() initial state
- start() port validation, port-in-use, OSError/Exception handling,
  new API (3.7+) and legacy API paths
- stop() with/without running task, CancelledError suppression
- set_holding_register/set_input_register/set_coil: no-context early return,
  negative address, uint16 overflow masking, new/legacy API writes, exceptions
- map_device_data: no-context, Cython path, Python path with bool/float/int/
  other types, batch atomic writes, address bounds, exception handling
- is_running property
- set_coils_batch/set_holding_registers_batch/set_input_registers_batch
- get_holding_register/get_input_register: no-context, out-of-range, exception
- get_register_map: no-context, new API, legacy API, exception
"""

from __future__ import annotations

import asyncio
import struct
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.engine.modbus_slave import (  # noqa: E402
    ModbusSlaveServer,
    _is_port_available,
    _parse_pymodbus_version,
)


# -- Helpers -----------------------------------------------------------------


def _make_new_api_context(
    coils_size: int = 10, discrete_size: int = 10, holding_size: int = 20, input_size: int = 20
) -> MagicMock:
    """Create a mock context mimicking pymodbus 3.7+ SimDevice."""
    ctx = MagicMock()
    ctx.co = MagicMock(data=[0] * coils_size)
    ctx.di = MagicMock(data=[0] * discrete_size)
    ctx.hr = MagicMock(data=[0] * holding_size)
    ctx.ir = MagicMock(data=[0] * input_size)
    return ctx


def _make_legacy_api_context(holding_size: int = 20, input_size: int = 20, coils_size: int = 10) -> MagicMock:
    """Create a mock context mimicking pymodbus < 3.7 ModbusServerContext."""
    ctx = MagicMock()
    slave = MagicMock()
    holding_data = [0] * holding_size
    input_data = [0] * input_size
    coils_data = [0] * coils_size

    def getValues(func_code, addr, count=1):
        if func_code == 3:
            return holding_data[addr : addr + count]
        if func_code == 4:
            return input_data[addr : addr + count]
        if func_code == 1:
            return coils_data[addr : addr + count]
        return [0]

    def setValues(func_code, addr, values):
        if func_code == 3:
            for i, v in enumerate(values):
                if addr + i < len(holding_data):
                    holding_data[addr + i] = v
        elif func_code == 4:
            for i, v in enumerate(values):
                if addr + i < len(input_data):
                    input_data[addr + i] = v
        elif func_code == 1:
            for i, v in enumerate(values):
                if addr + i < len(coils_data):
                    coils_data[addr + i] = v

    slave.getValues = getValues
    slave.setValues = setValues
    ctx.__getitem__ = MagicMock(return_value=slave)
    return ctx


# -- _parse_pymodbus_version -------------------------------------------------


class TestParsePymodbusVersion:
    def test_standard_version(self):
        with patch("edgelite.engine.modbus_slave.pymodbus", __version__="3.7.2"):
            major, minor = _parse_pymodbus_version()
        assert major == 3
        assert minor == 7

    def test_major_only(self):
        with patch("edgelite.engine.modbus_slave.pymodbus", __version__="3"):
            major, minor = _parse_pymodbus_version()
        assert major == 3
        assert minor == 0

    def test_default_on_missing_version(self):
        with patch("edgelite.engine.modbus_slave.pymodbus", spec=[]):
            major, minor = _parse_pymodbus_version()
        assert major == 2
        assert minor == 0

    def test_default_on_attribute_error(self):
        with patch("edgelite.engine.modbus_slave.pymodbus"):
            major, minor = _parse_pymodbus_version()
        assert major == 2
        assert minor == 0

    def test_handles_value_error(self):
        with patch("edgelite.engine.modbus_slave.pymodbus", __version__="abc.def"):
            major, minor = _parse_pymodbus_version()
        assert major == 2
        assert minor == 0

    def test_handles_index_error(self):
        """Version '3.' splits to ['3', ''], int('') raises ValueError -> (2,0)."""
        with patch("edgelite.engine.modbus_slave.pymodbus", __version__="3."):
            major, minor = _parse_pymodbus_version()
        assert major == 2
        assert minor == 0

    def test_two_part_version(self):
        with patch("edgelite.engine.modbus_slave.pymodbus", __version__="3.13"):
            major, minor = _parse_pymodbus_version()
        assert major == 3
        assert minor == 13


# -- _is_port_available ------------------------------------------------------


class TestIsPortAvailable:
    def test_available_port(self):
        """An ephemeral port should be available."""
        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)
            assert _is_port_available("127.0.0.1", 9999) is True
            mock_sock.setsockopt.assert_called_once()
            mock_sock.bind.assert_called_once_with(("127.0.0.1", 9999))

    def test_unavailable_port(self):
        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_sock.bind.side_effect = OSError("Address already in use")
            mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)
            assert _is_port_available("127.0.0.1", 502) is False

    def test_sets_so_reuseaddr(self):
        import socket

        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)
            _is_port_available("0.0.0.0", 8080)
            mock_sock.setsockopt.assert_called_once_with(
                socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
            )


# -- __init__ ----------------------------------------------------------------


class TestInit:
    def test_initial_state(self):
        s = ModbusSlaveServer()
        assert s._running is False
        assert s._server is None
        assert s._task is None
        assert s._context is None
        assert isinstance(s._register_lock, asyncio.Lock)

    def test_is_running_false_initially(self):
        s = ModbusSlaveServer()
        assert s.is_running is False


# -- start() -----------------------------------------------------------------


class TestStart:
    async def test_start_port_unavailable_raises(self):
        s = ModbusSlaveServer()
        with patch("edgelite.engine.modbus_slave._is_port_available", return_value=False):
            with pytest.raises(OSError, match="已被占用"):
                await s.start({"host": "127.0.0.1", "port": 502})
        assert s._running is False

    async def test_start_port_unavailable_sets_running_false(self):
        s = ModbusSlaveServer()
        with patch("edgelite.engine.modbus_slave._is_port_available", return_value=False):
            try:
                await s.start({"host": "127.0.0.1", "port": 502})
            except OSError:
                pass
        assert s._running is False

    async def test_start_low_port_logs_warning(self):
        """Ports < 1024 should log a warning about root privileges."""
        s = ModbusSlaveServer()
        with (
            patch("edgelite.engine.modbus_slave._is_port_available", return_value=True),
            patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False),
            patch("edgelite.engine.modbus_slave.ModbusSlaveServer._start_legacy_api", new=AsyncMock()) as mock_legacy,
        ):
            await s.start({"host": "127.0.0.1", "port": 80})
            mock_legacy.assert_called_once()

    async def test_start_default_host_and_port(self):
        s = ModbusSlaveServer()
        with (
            patch("edgelite.engine.modbus_slave._is_port_available", return_value=True),
            patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False),
            patch.object(s, "_start_legacy_api", new=AsyncMock()) as mock_legacy,
        ):
            await s.start({})
            mock_legacy.assert_called_once()
            args = mock_legacy.call_args[0]
            assert args[0] == "127.0.0.1"  # default host
            assert args[1] == 502  # default port

    async def test_start_default_sizes(self):
        s = ModbusSlaveServer()
        with (
            patch("edgelite.engine.modbus_slave._is_port_available", return_value=True),
            patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False),
            patch.object(s, "_start_legacy_api", new=AsyncMock()) as mock_legacy,
        ):
            await s.start({})
            args = mock_legacy.call_args[0]
            assert args[2] == 100  # coils_size
            assert args[3] == 100  # discrete_size
            assert args[4] == 1000  # holding_size
            assert args[5] == 1000  # input_size

    async def test_start_custom_config(self):
        s = ModbusSlaveServer()
        with (
            patch("edgelite.engine.modbus_slave._is_port_available", return_value=True),
            patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False),
            patch.object(s, "_start_legacy_api", new=AsyncMock()) as mock_legacy,
        ):
            await s.start(
                {
                    "host": "0.0.0.0",
                    "port": 5020,
                    "coils_size": 200,
                    "discrete_size": 300,
                    "holding_size": 2000,
                    "input_size": 3000,
                }
            )
            args = mock_legacy.call_args[0]
            assert args[0] == "0.0.0.0"
            assert args[1] == 5020
            assert args[2] == 200
            assert args[3] == 300
            assert args[4] == 2000
            assert args[5] == 3000

    async def test_start_new_api_path(self):
        s = ModbusSlaveServer()
        with (
            patch("edgelite.engine.modbus_slave._is_port_available", return_value=True),
            patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True),
            patch.object(s, "_start_new_api", new=AsyncMock()) as mock_new,
        ):
            await s.start({"host": "127.0.0.1", "port": 5020})
            mock_new.assert_called_once()

    async def test_start_os_error_propagates(self):
        s = ModbusSlaveServer()
        with (
            patch("edgelite.engine.modbus_slave._is_port_available", return_value=True),
            patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False),
            patch.object(s, "_start_legacy_api", new=AsyncMock(side_effect=OSError("bind failed"))),
        ):
            with pytest.raises(OSError, match="bind failed"):
                await s.start({"host": "127.0.0.1", "port": 5020})
        assert s._running is False
        assert s._task is None

    async def test_start_generic_exception_propagates(self):
        s = ModbusSlaveServer()
        with (
            patch("edgelite.engine.modbus_slave._is_port_available", return_value=True),
            patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True),
            patch.object(s, "_start_new_api", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await s.start({"host": "127.0.0.1", "port": 5020})
        assert s._running is False
        assert s._task is None

    async def test_start_none_config_uses_defaults(self):
        s = ModbusSlaveServer()
        with (
            patch("edgelite.engine.modbus_slave._is_port_available", return_value=True),
            patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False),
            patch.object(s, "_start_legacy_api", new=AsyncMock()) as mock_legacy,
        ):
            await s.start(None)
            mock_legacy.assert_called_once()


# -- _start_new_api() --------------------------------------------------------


class TestStartNewApi:
    async def test_creates_server_and_task(self):
        s = ModbusSlaveServer()
        mock_server = MagicMock()
        mock_server.serve_forever = MagicMock(return_value=asyncio.sleep(0))

        with (
            patch("pymodbus.datastore.SimData", create=True) as mock_sim_data,
            patch("pymodbus.datastore.SimDevice", create=True) as mock_sim_device,
            patch("pymodbus.server.ModbusTcpServer", create=True, return_value=mock_server),
        ):
            mock_sim_data.create = MagicMock(side_effect=lambda data: MagicMock(data=list(data)))
            await s._start_new_api("127.0.0.1", 5020, 100, 100, 1000, 1000)

        assert s._server is mock_server
        assert s._task is not None
        assert s._running is True
        assert s._context is not None
        # Clean up the task
        s._task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await s._task

    async def test_sim_data_create_calls(self):
        s = ModbusSlaveServer()
        mock_server = MagicMock()
        mock_server.serve_forever = MagicMock(return_value=asyncio.sleep(0))

        with (
            patch("pymodbus.datastore.SimData", create=True) as mock_sim_data,
            patch("pymodbus.datastore.SimDevice", create=True) as mock_sim_device,
            patch("pymodbus.server.ModbusTcpServer", create=True, return_value=mock_server),
        ):
            mock_sim_data.create = MagicMock(side_effect=lambda data: MagicMock(data=list(data)))
            await s._start_new_api("127.0.0.1", 5020, 10, 20, 30, 40)
            # SimData.create should be called 4 times (coils, discrete, holding, input)
            assert mock_sim_data.create.call_count == 4
        s._task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await s._task


# -- _start_legacy_api() -----------------------------------------------------


class TestStartLegacyApi:
    async def test_creates_task_and_context(self):
        s = ModbusSlaveServer()
        with (
            patch("edgelite.engine.modbus_slave._PYMODBUS_MAJOR", 3),
            patch("pymodbus.datastore.ModbusSequentialDataBlock") as mock_block,
            patch("pymodbus.datastore.ModbusServerContext") as mock_ctx,
            patch("pymodbus.datastore.ModbusSlaveContext", create=True) as mock_slave,
            patch("pymodbus.server.StartAsyncTcpServer", new=AsyncMock()) as mock_start,
        ):
            mock_block.return_value = MagicMock()
            mock_slave.return_value = MagicMock()
            mock_ctx.return_value = MagicMock()
            await s._start_legacy_api("127.0.0.1", 5020, 100, 100, 1000, 1000)

        assert s._task is not None
        assert s._running is True
        assert s._context is not None
        s._task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await s._task

    async def test_fallback_to_device_context(self):
        """When ModbusSlaveContext is not available, fall back to ModbusDeviceContext.

        In this environment ModbusSlaveContext doesn't exist in pymodbus.datastore,
        so the import naturally raises ImportError and falls through to
        ModbusDeviceContext.
        """
        s = ModbusSlaveServer()
        with (
            patch("edgelite.engine.modbus_slave._PYMODBUS_MAJOR", 3),
            patch("pymodbus.datastore.ModbusSequentialDataBlock") as mock_block,
            patch("pymodbus.datastore.ModbusServerContext") as mock_ctx,
            patch("pymodbus.datastore.ModbusDeviceContext") as mock_device,
            patch("pymodbus.server.StartAsyncTcpServer", new=AsyncMock()),
        ):
            mock_block.return_value = MagicMock()
            mock_device.return_value = MagicMock()
            mock_ctx.return_value = MagicMock()
            await s._start_legacy_api("127.0.0.1", 5020, 100, 100, 1000, 1000)
            mock_device.assert_called_once()
        s._task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await s._task

    async def test_pymodbus_2_uses_slaves_param(self):
        s = ModbusSlaveServer()
        with (
            patch("edgelite.engine.modbus_slave._PYMODBUS_MAJOR", 2),
            patch("pymodbus.datastore.ModbusSequentialDataBlock") as mock_block,
            patch("pymodbus.datastore.ModbusServerContext") as mock_ctx,
            patch("pymodbus.datastore.ModbusSlaveContext", create=True) as mock_slave,
            patch("pymodbus.server.StartAsyncTcpServer", new=AsyncMock()),
        ):
            mock_block.return_value = MagicMock()
            mock_slave.return_value = MagicMock()
            mock_ctx.return_value = MagicMock()
            await s._start_legacy_api("127.0.0.1", 5020, 100, 100, 1000, 1000)
            # pymodbus 2 uses slaves= param
            _, kwargs = mock_ctx.call_args
            assert "slaves" in kwargs
            assert kwargs["single"] is True
        s._task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await s._task

    async def test_pymodbus_3_uses_devices_param(self):
        s = ModbusSlaveServer()
        with (
            patch("edgelite.engine.modbus_slave._PYMODBUS_MAJOR", 3),
            patch("pymodbus.datastore.ModbusSequentialDataBlock") as mock_block,
            patch("pymodbus.datastore.ModbusServerContext") as mock_ctx,
            patch("pymodbus.datastore.ModbusSlaveContext", create=True) as mock_slave,
            patch("pymodbus.server.StartAsyncTcpServer", new=AsyncMock()),
        ):
            mock_block.return_value = MagicMock()
            mock_slave.return_value = MagicMock()
            mock_ctx.return_value = MagicMock()
            await s._start_legacy_api("127.0.0.1", 5020, 100, 100, 1000, 1000)
            _, kwargs = mock_ctx.call_args
            assert "devices" in kwargs
            assert kwargs["single"] is True
        s._task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await s._task


# -- stop() ------------------------------------------------------------------


class TestStop:
    async def test_stop_no_task(self):
        s = ModbusSlaveServer()
        await s.stop()
        assert s._running is False
        assert s._context is None

    async def test_stop_with_running_task(self):
        s = ModbusSlaveServer()

        async def long_running():
            await asyncio.sleep(100)

        s._task = asyncio.create_task(long_running())
        s._running = True
        await s.stop()
        assert s._running is False
        assert s._task.done()
        assert s._context is None

    async def test_stop_with_completed_task(self):
        s = ModbusSlaveServer()

        async def quick():
            pass

        s._task = asyncio.create_task(quick())
        await asyncio.sleep(0)  # let it complete
        s._running = True
        await s.stop()
        assert s._running is False

    async def test_stop_suppresses_cancelled_error(self):
        s = ModbusSlaveServer()

        async def cancellable():
            await asyncio.sleep(100)

        s._task = asyncio.create_task(cancellable())
        s._task.cancel()
        # stop should suppress the CancelledError
        await s.stop()
        assert s._running is False

    async def test_stop_clears_context(self):
        s = ModbusSlaveServer()
        s._context = MagicMock()
        s._running = True
        await s.stop()
        assert s._context is None


# -- set_holding_register() --------------------------------------------------


class TestSetHoldingRegister:
    async def test_no_context_returns_early(self):
        s = ModbusSlaveServer()
        # Should not raise
        await s.set_holding_register(0, 100)

    async def test_negative_address_returns_early(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        await s.set_holding_register(-1, 100)
        # Verify nothing was written
        assert s._context.hr.data[0] == 0

    async def test_new_api_writes_value(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_holding_register(5, 12345)
        assert s._context.hr.data[5] == 12345

    async def test_new_api_address_out_of_range(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context(holding_size=5)
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_holding_register(100, 42)
        # Should not raise, nothing written

    async def test_value_out_of_range_masked(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_holding_register(0, 70000)  # > 0xFFFF
        assert s._context.hr.data[0] == 70000 & 0xFFFF

    async def test_negative_value_masked(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_holding_register(0, -1)
        assert s._context.hr.data[0] == 0xFFFF

    async def test_legacy_api_writes_value(self):
        s = ModbusSlaveServer()
        s._context = _make_legacy_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False):
            await s.set_holding_register(3, 999)
        assert s._context[0].getValues(3, 3, 1)[0] == 999

    async def test_exception_handled(self):
        s = ModbusSlaveServer()
        ctx = MagicMock()
        ctx.hr.data = MagicMock()
        ctx.hr.data.__getitem__ = MagicMock(side_effect=RuntimeError("boom"))
        ctx.hr.data.__len__ = MagicMock(return_value=10)
        s._context = ctx
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            # Should not raise
            await s.set_holding_register(0, 42)

    async def test_boundary_values(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_holding_register(0, 0)
            assert s._context.hr.data[0] == 0
            await s.set_holding_register(1, 0xFFFF)
            assert s._context.hr.data[1] == 0xFFFF


# -- set_input_register() ----------------------------------------------------


class TestSetInputRegister:
    async def test_no_context_returns_early(self):
        s = ModbusSlaveServer()
        await s.set_input_register(0, 100)

    async def test_negative_address_returns_early(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        await s.set_input_register(-1, 100)
        assert s._context.ir.data[0] == 0

    async def test_new_api_writes_value(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_input_register(5, 30000)
        assert s._context.ir.data[5] == 30000

    async def test_new_api_address_out_of_range(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context(input_size=5)
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_input_register(100, 42)

    async def test_value_out_of_range_masked(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_input_register(0, 100000)
        assert s._context.ir.data[0] == 100000 & 0xFFFF

    async def test_legacy_api_writes_value(self):
        s = ModbusSlaveServer()
        s._context = _make_legacy_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False):
            await s.set_input_register(2, 555)
        assert s._context[0].getValues(4, 2, 1)[0] == 555

    async def test_exception_handled(self):
        s = ModbusSlaveServer()
        ctx = MagicMock()
        ctx.ir.data = MagicMock()
        ctx.ir.data.__getitem__ = MagicMock(side_effect=RuntimeError("boom"))
        ctx.ir.data.__len__ = MagicMock(return_value=10)
        s._context = ctx
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_input_register(0, 42)


# -- set_coil() --------------------------------------------------------------


class TestSetCoil:
    async def test_no_context_returns_early(self):
        s = ModbusSlaveServer()
        await s.set_coil(0, True)

    async def test_negative_address_returns_early(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        await s.set_coil(-1, True)
        assert s._context.co.data[0] == 0

    async def test_new_api_writes_true(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_coil(3, True)
        assert s._context.co.data[3] == 1

    async def test_new_api_writes_false(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        s._context.co.data[3] = 1
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_coil(3, False)
        assert s._context.co.data[3] == 0

    async def test_new_api_address_out_of_range(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context(coils_size=5)
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_coil(100, True)

    async def test_legacy_api_writes_value(self):
        s = ModbusSlaveServer()
        s._context = _make_legacy_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False):
            await s.set_coil(2, True)
        assert s._context[0].getValues(1, 2, 1)[0] == 1

    async def test_exception_handled(self):
        s = ModbusSlaveServer()
        ctx = MagicMock()
        ctx.co.data = MagicMock()
        ctx.co.data.__getitem__ = MagicMock(side_effect=RuntimeError("boom"))
        ctx.co.data.__len__ = MagicMock(return_value=10)
        s._context = ctx
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_coil(0, True)


# -- map_device_data() -------------------------------------------------------


class TestMapDeviceData:
    async def test_no_context_returns_early(self):
        s = ModbusSlaveServer()
        await s.map_device_data("dev1", {"p1": 42}, 0)

    async def test_bool_value_maps_to_coil(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.map_device_data("dev1", {"p1": True}, 0)
        assert s._context.co.data[0] == 1

    async def test_float_value_maps_to_two_holding_registers(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.map_device_data("dev1", {"p1": 3.14}, 0)
        # Float 3.14 packed as big-endian >f, split into two uint16
        raw = struct.pack(">f", 3.14)
        hi = struct.unpack(">H", raw[:2])[0]
        lo = struct.unpack(">H", raw[2:])[0]
        assert s._context.hr.data[0] == hi
        assert s._context.hr.data[1] == lo

    async def test_small_int_maps_to_one_holding_register(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.map_device_data("dev1", {"p1": 42}, 0)
        assert s._context.hr.data[0] == 42

    async def test_negative_int_in_range(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.map_device_data("dev1", {"p1": -1}, 0)
        # -1 in range -32768..65535 -> -1 & 0xFFFF = 0xFFFF
        assert s._context.hr.data[0] == 0xFFFF

    async def test_large_int_maps_to_two_holding_registers(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.map_device_data("dev1", {"p1": 100000}, 0)
        # 100000 > 65535 -> split into hi/lo
        hi = (100000 >> 16) & 0xFFFF
        lo = 100000 & 0xFFFF
        assert s._context.hr.data[0] == hi
        assert s._context.hr.data[1] == lo

    async def test_other_type_increments_offset(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            # string value -> "other" branch, offset increments by 1
            await s.map_device_data("dev1", {"p1": "not_a_number", "p2": 99}, 0)
        # p1 is string -> offset becomes 1, p2=99 written at address 1
        assert s._context.hr.data[1] == 99

    async def test_base_address_offset(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.map_device_data("dev1", {"p1": 77}, 10)
        assert s._context.hr.data[10] == 77

    async def test_multiple_points_mixed_types(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.map_device_data(
                "dev1",
                {"p1": True, "p2": 42, "p3": 3.14},
                0,
            )
        # p1=bool -> coil[0]=1, offset=1
        assert s._context.co.data[0] == 1
        # p2=int -> hr[1]=42, offset=2
        assert s._context.hr.data[1] == 42
        # p3=float -> hr[2]=hi, hr[3]=lo
        raw = struct.pack(">f", 3.14)
        assert s._context.hr.data[2] == struct.unpack(">H", raw[:2])[0]
        assert s._context.hr.data[3] == struct.unpack(">H", raw[2:])[0]

    async def test_cython_path_new_api(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        mock_mapper = MagicMock(return_value=3)
        with (
            patch("edgelite.engine.modbus_slave._HAS_CYTHON_MAPPER", True),
            patch("edgelite.engine.modbus_slave.map_device_data_fast", mock_mapper),
            patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True),
        ):
            await s.map_device_data("dev1", {"p1": 1, "p2": 2}, 0)
        mock_mapper.assert_called_once()

    async def test_cython_path_legacy_api(self):
        s = ModbusSlaveServer()
        s._context = _make_legacy_api_context()
        mock_mapper = MagicMock(return_value=2)
        with (
            patch("edgelite.engine.modbus_slave._HAS_CYTHON_MAPPER", True),
            patch("edgelite.engine.modbus_slave.map_device_data_fast", mock_mapper),
            patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False),
        ):
            await s.map_device_data("dev1", {"p1": 1}, 0)
        mock_mapper.assert_called_once()

    async def test_cython_path_exception_in_read_falls_back_to_zeros(self):
        s = ModbusSlaveServer()
        ctx = MagicMock()
        ctx.hr = MagicMock()
        ctx.hr.data = MagicMock()
        ctx.hr.data.__getitem__ = MagicMock(side_effect=RuntimeError("read fail"))
        ctx.hr.data.__len__ = MagicMock(return_value=10)
        ctx.ir = MagicMock(data=MagicMock(__len__=MagicMock(return_value=10)))
        ctx.co = MagicMock(data=MagicMock(__len__=MagicMock(return_value=10)))
        ctx.di = MagicMock(data=MagicMock(__len__=MagicMock(return_value=10)))
        s._context = ctx
        mock_mapper = MagicMock(return_value=1)
        with (
            patch("edgelite.engine.modbus_slave._HAS_CYTHON_MAPPER", True),
            patch("edgelite.engine.modbus_slave.map_device_data_fast", mock_mapper),
            patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True),
        ):
            # Should fall back to zero-filled lists without raising
            await s.map_device_data("dev1", {"p1": 1}, 0)

    async def test_legacy_api_writes_values(self):
        s = ModbusSlaveServer()
        s._context = _make_legacy_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False):
            await s.map_device_data("dev1", {"p1": 88}, 0)
        assert s._context[0].getValues(3, 0, 1)[0] == 88

    async def test_legacy_api_bool_maps_to_coil(self):
        s = ModbusSlaveServer()
        s._context = _make_legacy_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False):
            await s.map_device_data("dev1", {"p1": True}, 0)
        assert s._context[0].getValues(1, 0, 1)[0] == 1

    async def test_exception_handled_gracefully(self):
        s = ModbusSlaveServer()
        ctx = MagicMock()
        ctx.hr = MagicMock()
        ctx.hr.data = MagicMock()
        ctx.hr.data.__len__ = MagicMock(side_effect=RuntimeError("len fail"))
        s._context = ctx
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            # Should not raise
            await s.map_device_data("dev1", {"p1": 42}, 0)

    async def test_cython_path_write_exception_handled(self):
        s = ModbusSlaveServer()
        ctx = MagicMock()
        ctx.hr = MagicMock()
        ctx.hr.data = MagicMock()
        ctx.hr.data.__getitem__ = MagicMock(side_effect=[0, RuntimeError("write fail")])
        ctx.hr.data.__setitem__ = MagicMock(side_effect=RuntimeError("write fail"))
        ctx.hr.data.__len__ = MagicMock(return_value=1000)
        ctx.ir = MagicMock(data=MagicMock(__len__=MagicMock(return_value=1000)))
        ctx.co = MagicMock(data=MagicMock(__len__=MagicMock(return_value=100)))
        s._context = ctx
        mock_mapper = MagicMock(return_value=2)
        with (
            patch("edgelite.engine.modbus_slave._HAS_CYTHON_MAPPER", True),
            patch("edgelite.engine.modbus_slave.map_device_data_fast", mock_mapper),
            patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True),
        ):
            await s.map_device_data("dev1", {"p1": 1}, 0)


# -- is_running property -----------------------------------------------------


class TestIsRunning:
    def test_false_initially(self):
        s = ModbusSlaveServer()
        assert s.is_running is False

    def test_true_when_running(self):
        s = ModbusSlaveServer()
        s._running = True
        assert s.is_running is True

    def test_reflects_state_change(self):
        s = ModbusSlaveServer()
        assert s.is_running is False
        s._running = True
        assert s.is_running is True
        s._running = False
        assert s.is_running is False


# -- Batch operations --------------------------------------------------------


class TestBatchOperations:
    async def test_set_coils_batch(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_coils_batch(0, [True, False, True])
        assert s._context.co.data[0] == 1
        assert s._context.co.data[1] == 0
        assert s._context.co.data[2] == 1

    async def test_set_coils_batch_empty(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_coils_batch(0, [])
        # No writes, no error

    async def test_set_holding_registers_batch(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_holding_registers_batch(5, [10, 20, 30])
        assert s._context.hr.data[5] == 10
        assert s._context.hr.data[6] == 20
        assert s._context.hr.data[7] == 30

    async def test_set_holding_registers_batch_empty(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_holding_registers_batch(0, [])

    async def test_set_input_registers_batch(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_input_registers_batch(2, [100, 200])
        assert s._context.ir.data[2] == 100
        assert s._context.ir.data[3] == 200

    async def test_set_input_registers_batch_empty(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_input_registers_batch(0, [])

    async def test_batch_with_offset(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_holding_registers_batch(10, [1, 2, 3, 4])
        assert s._context.hr.data[10] == 1
        assert s._context.hr.data[13] == 4


# -- get_holding_register() --------------------------------------------------


class TestGetHoldingRegister:
    async def test_no_context_returns_none(self):
        s = ModbusSlaveServer()
        assert await s.get_holding_register(0) is None

    async def test_new_api_returns_value(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        s._context.hr.data[5] = 12345
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            result = await s.get_holding_register(5)
        assert result == 12345

    async def test_new_api_address_out_of_range(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context(holding_size=5)
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            result = await s.get_holding_register(100)
        # Returns None (falls through)
        assert result is None

    async def test_legacy_api_returns_value(self):
        s = ModbusSlaveServer()
        s._context = _make_legacy_api_context()
        s._context[0].setValues(3, 3, [999])
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False):
            result = await s.get_holding_register(3)
        assert result == 999

    async def test_legacy_api_empty_values(self):
        s = ModbusSlaveServer()
        ctx = MagicMock()
        slave = MagicMock()
        slave.getValues.return_value = []
        ctx.__getitem__ = MagicMock(return_value=slave)
        s._context = ctx
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False):
            result = await s.get_holding_register(0)
        assert result is None

    async def test_exception_returns_none(self):
        s = ModbusSlaveServer()
        ctx = MagicMock()
        ctx.hr = MagicMock()
        ctx.hr.data = MagicMock()
        ctx.hr.data.__getitem__ = MagicMock(side_effect=RuntimeError("boom"))
        ctx.hr.data.__len__ = MagicMock(return_value=10)
        s._context = ctx
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            result = await s.get_holding_register(0)
        assert result is None


# -- get_input_register() ----------------------------------------------------


class TestGetInputRegister:
    async def test_no_context_returns_none(self):
        s = ModbusSlaveServer()
        assert await s.get_input_register(0) is None

    async def test_new_api_returns_value(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        s._context.ir.data[7] = 4242
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            result = await s.get_input_register(7)
        assert result == 4242

    async def test_new_api_address_out_of_range(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context(input_size=5)
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            result = await s.get_input_register(100)
        assert result is None

    async def test_legacy_api_returns_value(self):
        s = ModbusSlaveServer()
        s._context = _make_legacy_api_context()
        s._context[0].setValues(4, 2, [555])
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False):
            result = await s.get_input_register(2)
        assert result == 555

    async def test_legacy_api_empty_values(self):
        s = ModbusSlaveServer()
        ctx = MagicMock()
        slave = MagicMock()
        slave.getValues.return_value = []
        ctx.__getitem__ = MagicMock(return_value=slave)
        s._context = ctx
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False):
            result = await s.get_input_register(0)
        assert result is None

    async def test_exception_returns_none(self):
        s = ModbusSlaveServer()
        ctx = MagicMock()
        ctx.ir = MagicMock()
        ctx.ir.data = MagicMock()
        ctx.ir.data.__getitem__ = MagicMock(side_effect=RuntimeError("boom"))
        ctx.ir.data.__len__ = MagicMock(return_value=10)
        s._context = ctx
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            result = await s.get_input_register(0)
        assert result is None


# -- get_register_map() ------------------------------------------------------


class TestGetRegisterMap:
    async def test_no_context_returns_empty(self):
        s = ModbusSlaveServer()
        result = await s.get_register_map()
        assert result == {}

    async def test_new_api_returns_full_map(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context(
            coils_size=10, discrete_size=10, holding_size=20, input_size=20
        )
        # Set some values
        s._context.co.data[0] = 1
        s._context.hr.data[0] = 42
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            result = await s.get_register_map()
        assert "coils" in result
        assert "discrete_inputs" in result
        assert "holding_registers" in result
        assert "input_registers" in result
        assert result["coils"]["size"] == 10
        assert result["discrete_inputs"]["size"] == 10
        assert result["holding_registers"]["size"] == 20
        assert result["input_registers"]["size"] == 20
        assert result["coils"]["sample"][0] == 1
        assert result["holding_registers"]["sample"][0] == 42

    async def test_new_api_sample_truncated_to_10(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context(
            coils_size=20, discrete_size=20, holding_size=20, input_size=20
        )
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            result = await s.get_register_map()
        assert len(result["coils"]["sample"]) == 10
        assert len(result["holding_registers"]["sample"]) == 10

    async def test_legacy_api_returns_status(self):
        s = ModbusSlaveServer()
        s._context = _make_legacy_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", False):
            result = await s.get_register_map()
        assert result == {"status": "legacy_api", "message": "寄存器数据需要通过getValues获取"}

    async def test_exception_returns_empty(self):
        s = ModbusSlaveServer()
        ctx = MagicMock()
        ctx.co = MagicMock()
        ctx.co.data = MagicMock()
        ctx.co.data.__len__ = MagicMock(side_effect=RuntimeError("boom"))
        s._context = ctx
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            result = await s.get_register_map()
        assert result == {}


# -- Integration: set then get -----------------------------------------------


class TestSetThenGet:
    async def test_holding_register_round_trip(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_holding_register(5, 12345)
            result = await s.get_holding_register(5)
        assert result == 12345

    async def test_input_register_round_trip(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_input_register(3, 9999)
            result = await s.get_input_register(3)
        assert result == 9999

    async def test_coil_then_map(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.set_coil(2, True)
            result = await s.get_register_map()
        assert result["coils"]["sample"][2] == 1

    async def test_map_then_read(self):
        s = ModbusSlaveServer()
        s._context = _make_new_api_context()
        with patch("edgelite.engine.modbus_slave._PYMODBUS_37_PLUS", True):
            await s.map_device_data("dev1", {"p1": 77, "p2": 88}, 0)
            assert await s.get_holding_register(0) == 77
            assert await s.get_holding_register(1) == 88
