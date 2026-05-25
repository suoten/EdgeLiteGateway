"""Notification module - supports DingTalk/WeCom/Email/Webhook channels

This module provides multi-channel alarm notification capabilities.
"""

from edgelite.services.notification_impl import (
    AlarmNotification,
    NotificationChannel,
    NotificationManager,
    DingTalkChannel,
    WeComChannel,
    EmailChannel,
    WebhookChannel,
    DingTalkConfig,
    WeComConfig,
    EmailConfig,
    WebhookConfig,
    get_notification_manager,
    init_notification_manager,
)

__all__ = [
    "AlarmNotification",
    "NotificationChannel",
    "NotificationManager",
    "DingTalkChannel",
    "WeComChannel",
    "EmailChannel",
    "WebhookChannel",
    "DingTalkConfig",
    "WeComConfig",
    "EmailConfig",
    "WebhookConfig",
    "get_notification_manager",
    "init_notification_manager",
]
