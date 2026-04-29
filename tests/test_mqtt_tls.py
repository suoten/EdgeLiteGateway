"""MQTT TLS配置单元测试"""
import pytest
import sys
sys.path.insert(0, 'src')

from edgelite.engine.mqtt_tls import MqttTlsHelper


class TestMqttTls:
    def test_no_certs_returns_none(self):
        """无证书配置返回None"""
        result = MqttTlsHelper.create_ssl_context()
        assert result is None

    def test_validate_cert_file_empty_path(self):
        """空路径验证失败"""
        assert MqttTlsHelper.validate_cert_file("") is False

    def test_validate_cert_file_nonexistent(self):
        """不存在文件验证失败"""
        assert MqttTlsHelper.validate_cert_file("/nonexistent/cert.pem") is False

    def test_cert_reqs_modes(self):
        """cert_reqs模式名称"""
        try:
            MqttTlsHelper.create_ssl_context(cert_reqs="none")
        except Exception:
            pass
