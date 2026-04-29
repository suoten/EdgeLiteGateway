"""Webhook认证中间件单元测试"""
import pytest
import base64
import os
import sys
sys.path.insert(0, 'src')

from edgelite.engine.webhook_auth import WebhookAuthMiddleware


class TestWebhookAuth:
    def test_none_mode_always_pass(self):
        """none模式始终通过"""
        mw = WebhookAuthMiddleware(mode="none")
        assert mw.verify(None) is True
        assert mw.verify("anything") is True

    def test_bearer_correct(self):
        """Bearer Token正确"""
        mw = WebhookAuthMiddleware(mode="bearer", token="my-secret")
        assert mw.verify("Bearer my-secret") is True

    def test_bearer_incorrect(self):
        """Bearer Token错误"""
        mw = WebhookAuthMiddleware(mode="bearer", token="my-secret")
        assert mw.verify("Bearer wrong") is False

    def test_bearer_missing_header(self):
        """Bearer无header"""
        mw = WebhookAuthMiddleware(mode="bearer", token="my-secret")
        assert mw.verify(None) is False

    def test_bearer_wrong_prefix(self):
        """Bearer前缀错误"""
        mw = WebhookAuthMiddleware(mode="bearer", token="my-secret")
        assert mw.verify("Basic abc") is False

    def test_basic_correct(self):
        """Basic Auth正确"""
        mw = WebhookAuthMiddleware(mode="basic", username="admin", password="pass")
        cred = base64.b64encode(b"admin:pass").decode()
        assert mw.verify(f"Basic {cred}") is True

    def test_basic_incorrect(self):
        """Basic Auth错误"""
        mw = WebhookAuthMiddleware(mode="basic", username="admin", password="pass")
        cred = base64.b64encode(b"admin:wrong").decode()
        assert mw.verify(f"Basic {cred}") is False

    def test_env_var_resolution(self):
        """环境变量解析"""
        os.environ["TEST_WEBHOOK_TOKEN"] = "env-token-123"
        mw = WebhookAuthMiddleware(mode="bearer", token="${TEST_WEBHOOK_TOKEN}")
        assert mw.verify("Bearer env-token-123") is True
        del os.environ["TEST_WEBHOOK_TOKEN"]

    def test_unknown_mode(self):
        """未知模式"""
        mw = WebhookAuthMiddleware(mode="unknown")
        assert mw.verify("anything") is False
