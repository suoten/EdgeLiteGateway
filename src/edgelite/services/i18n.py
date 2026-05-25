"""Internationalization (i18n) module - Chinese/English support

This module provides bilingual (Chinese/English) support for the application.
Language can be switched via configuration or Accept-Language header.
"""

from __future__ import annotations

import gettext
import locale
import os
from dataclasses import dataclass
from typing import Callable

# Default language
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ["en", "zh"]


@dataclass
class TranslationStrings:
    """Container for all translatable strings"""
    # System
    APP_NAME: str = "EdgeLiteGateway"
    VERSION: str = "Version"

    # Device status
    STATUS_ONLINE: str = "Online"
    STATUS_OFFLINE: str = "Offline"
    STATUS_DEGRADED: str = "Degraded"

    # Alarm severity
    SEVERITY_CRITICAL: str = "Critical"
    SEVERITY_MAJOR: str = "Major"
    SEVERITY_MINOR: str = "Minor"
    SEVERITY_WARNING: str = "Warning"
    SEVERITY_INFO: str = "Info"

    # Alarm actions
    ACTION_FIRING: str = "Alarm Triggered"
    ACTION_RECOVERED: str = "Alarm Recovered"
    ACTION_ACKNOWLEDGED: str = "Alarm Acknowledged"
    ACTION_ESCALATED: str = "Alarm Escalated"

    # Common
    DEVICE: str = "Device"
    RULE: str = "Rule"
    ALARM: str = "Alarm"
    USER: str = "User"
    SETTINGS: str = "Settings"
    SAVE: str = "Save"
    CANCEL: str = "Cancel"
    DELETE: str = "Delete"
    EDIT: str = "Edit"
    CREATE: str = "Create"
    SEARCH: str = "Search"
    REFRESH: str = "Refresh"
    EXPORT: str = "Export"
    IMPORT: str = "Import"

    # Error messages
    ERR_CONNECTION_FAILED: str = "Connection failed"
    ERR_TIMEOUT: str = "Request timeout"
    ERR_AUTH_FAILED: str = "Authentication failed"
    ERR_PERMISSION_DENIED: str = "Permission denied"
    ERR_NOT_FOUND: str = "Resource not found"
    ERR_VALIDATION_FAILED: str = "Validation failed"
    ERR_INTERNAL_ERROR: str = "Internal server error"

    # Device management
    DEVICE_ID: str = "Device ID"
    DEVICE_NAME: str = "Device Name"
    PROTOCOL: str = "Protocol"
    COLLECT_INTERVAL: str = "Collect Interval"
    POINTS_COUNT: str = "Points Count"
    ADD_DEVICE: str = "Add Device"
    DELETE_DEVICE: str = "Delete Device"
    DEVICE_TEMPLATES: str = "Device Templates"
    DEVICE_GROUPS: str = "Device Groups"

    # Rule management
    RULE_ID: str = "Rule ID"
    RULE_NAME: str = "Rule Name"
    CONDITIONS: str = "Conditions"
    SEVERITY: str = "Severity"
    DURATION: str = "Duration"
    ADD_RULE: str = "Add Rule"
    DELETE_RULE: str = "Delete Rule"
    RULE_ENABLED: str = "Enabled"
    RULE_DISABLED: str = "Disabled"

    # Alarm management
    ALARM_ID: str = "Alarm ID"
    TRIGGER_VALUE: str = "Trigger Value"
    FIRED_AT: str = "Fired At"
    RECOVERED_AT: str = "Recovered At"
    ACKNOWLEDGED_BY: str = "Acknowledged By"
    ACK_ALARM: str = "Acknowledge Alarm"
    CLEAR_ALARM: str = "Clear Alarm"
    ALARM_STATISTICS: str = "Alarm Statistics"
    MTTR: str = "MTTR (Mean Time To Recovery)"
    MTBF: str = "MTBF (Mean Time Between Failures)"
    ALARM_COUNT: str = "Alarm Count"
    ALARM_TREND: str = "Alarm Trend"

    # Notification channels
    NOTIFICATION_CHANNELS: str = "Notification Channels"
    DINGTALK: str = "DingTalk"
    WECOM: str = "WeCom (Enterprise WeChat)"
    EMAIL: str = "Email"
    WEBHOOK: str = "Webhook"
    CHANNEL_ENABLED: str = "Channel Enabled"
    CHANNEL_DISABLED: str = "Channel Disabled"
    TEST_CHANNEL: str = "Test Channel"
    CONFIGURE_CHANNEL: str = "Configure Channel"

    # System management
    SYSTEM_CONFIG: str = "System Configuration"
    BACKUP_CONFIG: str = "Backup Configuration"
    RESTORE_CONFIG: str = "Restore Configuration"
    LOG_SETTINGS: str = "Log Settings"
    LOG_LEVEL: str = "Log Level"
    LOG_ROTATION: str = "Log Rotation"
    MAX_LOG_SIZE: str = "Max Log Size"
    BACKUP_COUNT: str = "Backup Count"

    # Data management
    DATA_QUERY: str = "Data Query"
    TIME_RANGE: str = "Time Range"
    AGGREGATION: str = "Aggregation"
    EXPORT_DATA: str = "Export Data"
    IMPORT_DATA: str = "Import Data"

    # Command control
    COMMAND_CONTROL: str = "Command Control"
    SEND_COMMAND: str = "Send Command"
    COMMAND_HISTORY: str = "Command History"
    APPROVAL_REQUIRED: str = "Approval Required"
    PENDING_APPROVAL: str = "Pending Approval"


# English translations
ENGLISH_STRINGS = TranslationStrings()


# Chinese translations
ZH_CHINESE_STRINGS = TranslationStrings(
    # System
    APP_NAME="EdgeLiteGateway",
    VERSION="版本",

    # Device status
    STATUS_ONLINE="在线",
    STATUS_OFFLINE="离线",
    STATUS_DEGRADED="降级",

    # Alarm severity
    SEVERITY_CRITICAL="紧急",
    SEVERITY_MAJOR="重要",
    SEVERITY_MINOR="次要",
    SEVERITY_WARNING="警告",
    SEVERITY_INFO="信息",

    # Alarm actions
    ACTION_FIRING="告警触发",
    ACTION_RECOVERED="告警恢复",
    ACTION_ACKNOWLEDGED="告警确认",
    ACTION_ESCALATED="告警升级",

    # Common
    DEVICE="设备",
    RULE="规则",
    ALARM="告警",
    USER="用户",
    SETTINGS="设置",
    SAVE="保存",
    CANCEL="取消",
    DELETE="删除",
    EDIT="编辑",
    CREATE="创建",
    SEARCH="搜索",
    REFRESH="刷新",
    EXPORT="导出",
    IMPORT="导入",

    # Error messages
    ERR_CONNECTION_FAILED="连接失败",
    ERR_TIMEOUT="请求超时",
    ERR_AUTH_FAILED="认证失败",
    ERR_PERMISSION_DENIED="权限不足",
    ERR_NOT_FOUND="资源未找到",
    ERR_VALIDATION_FAILED="验证失败",
    ERR_INTERNAL_ERROR="服务器内部错误",

    # Device management
    DEVICE_ID="设备ID",
    DEVICE_NAME="设备名称",
    PROTOCOL="协议",
    COLLECT_INTERVAL="采集间隔",
    POINTS_COUNT="测点数量",
    ADD_DEVICE="添加设备",
    DELETE_DEVICE="删除设备",
    DEVICE_TEMPLATES="设备模板",
    DEVICE_GROUPS="设备分组",

    # Rule management
    RULE_ID="规则ID",
    RULE_NAME="规则名称",
    CONDITIONS="条件",
    SEVERITY="严重程度",
    DURATION="持续时间",
    ADD_RULE="添加规则",
    DELETE_RULE="删除规则",
    RULE_ENABLED="已启用",
    RULE_DISABLED="已禁用",

    # Alarm management
    ALARM_ID="告警ID",
    TRIGGER_VALUE="触发值",
    FIRED_AT="触发时间",
    RECOVERED_AT="恢复时间",
    ACKNOWLEDGED_BY="确认人",
    ACK_ALARM="确认告警",
    CLEAR_ALARM="清除告警",
    ALARM_STATISTICS="告警统计",
    MTTR="平均恢复时间",
    MTBF="平均故障间隔时间",
    ALARM_COUNT="告警数量",
    ALARM_TREND="告警趋势",

    # Notification channels
    NOTIFICATION_CHANNELS="通知渠道",
    DINGTALK="钉钉",
    WECOM="企业微信",
    EMAIL="邮件",
    WEBHOOK="Webhook",
    CHANNEL_ENABLED="渠道已启用",
    CHANNEL_DISABLED="渠道已禁用",
    TEST_CHANNEL="测试渠道",
    CONFIGURE_CHANNEL="配置渠道",

    # System management
    SYSTEM_CONFIG="系统配置",
    BACKUP_CONFIG="备份配置",
    RESTORE_CONFIG="恢复配置",
    LOG_SETTINGS="日志设置",
    LOG_LEVEL="日志级别",
    LOG_ROTATION="日志轮转",
    MAX_LOG_SIZE="最大日志大小",
    BACKUP_COUNT="备份数量",

    # Data management
    DATA_QUERY="数据查询",
    TIME_RANGE="时间范围",
    AGGREGATION="聚合",
    EXPORT_DATA="导出数据",
    IMPORT_DATA="导入数据",

    # Command control
    COMMAND_CONTROL="指令控制",
    SEND_COMMAND="发送指令",
    COMMAND_HISTORY="指令历史",
    APPROVAL_REQUIRED="需要审批",
    PENDING_APPROVAL="待审批",
)


# Translation map
TRANSLATIONS: dict[str, TranslationStrings] = {
    "en": ENGLISH_STRINGS,
    "zh": ZH_CHINESE_STRINGS,
}


class I18n:
    """Internationalization manager"""

    def __init__(self, language: str = DEFAULT_LANGUAGE):
        self._language = language
        self._callbacks: list[Callable[[str], None]] = []

    @property
    def language(self) -> str:
        return self._language

    @property
    def strings(self) -> TranslationStrings:
        return TRANSLATIONS.get(self._language, ENGLISH_STRINGS)

    def set_language(self, language: str) -> bool:
        """Set the current language"""
        if language not in SUPPORTED_LANGUAGES:
            return False
        self._language = language
        self._notify_change()
        return True

    def get_language_from_request(self, accept_language: str | None = None) -> str:
        """Determine language from Accept-Language header"""
        if not accept_language:
            return self._language

        # Parse Accept-Language header
        languages = []
        for part in accept_language.split(","):
            part = part.strip().split(";")[0]
            if part:
                languages.append(part.lower())

        # Match supported languages
        for lang in languages:
            if lang.startswith("zh"):
                return "zh"
            elif lang.startswith("en"):
                return "en"

        return self._language

    def register_change_callback(self, callback: Callable[[str], None]) -> None:
        """Register a callback for language change events"""
        self._callbacks.append(callback)

    def _notify_change(self) -> None:
        """Notify all registered callbacks of language change"""
        for callback in self._callbacks:
            try:
                callback(self._language)
            except Exception:
                pass

    def t(self, key: str) -> str:
        """Get translation for a key"""
        try:
            return getattr(self.strings, key, key)
        except AttributeError:
            return key


# Global i18n instance
_i18n: I18n | None = None


def get_i18n() -> I18n:
    """Get the global i18n instance"""
    global _i18n
    if _i18n is None:
        _i18n = I18n()
    return _i18n


def set_language(language: str) -> bool:
    """Set the global language"""
    return get_i18n().set_language(language)


def get_strings() -> TranslationStrings:
    """Get the current translation strings"""
    return get_i18n().strings


def t(key: str) -> str:
    """Get translation for a key (shorthand)"""
    return get_i18n().t(key)


def get_translatable_keys() -> list[str]:
    """Get all available translation keys"""
    return [attr for attr in dir(TranslationStrings) if not attr.startswith("_")]
