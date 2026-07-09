"""Business services layer"""

from edgelite.services.alarm_service import AlarmService
from edgelite.services.notification import (
    NotificationManager,
    get_notification_manager,
    init_notification_manager,
)
from edgelite.services.i18n import (
    I18n,
    get_i18n,
    set_language,
    get_strings,
    t,
)
from edgelite.services.data_import_export import (
    DataExportService,
    DataImportService,
    TemplateService,
    ExportFormat,
    ImportMode,
    DeviceTemplate,
    RuleTemplate,
    DeviceGroup,
    get_export_service,
    get_import_service,
    get_template_service,
)
from edgelite.services.historical_data import (
    HistoricalDataService,
    DeviceShadowService,
    AggregationType,
    QueryOptions,
    get_historical_service,
    get_shadow_service,
)
from edgelite.services.command_approval import (
    CommandApprovalService,
    CommandRequest,
    ApprovalChain,
    ApprovalStatus,
    get_approval_service,
)
from edgelite.services.system_services import (
    ConfigBackupService,
    LogRotationService,
    ConfigAuditService,
    ConfigValidator,
    LogConfig,
    BackupMetadata,
    AuditEntry,
    get_backup_service,
    get_log_rotation_service,
    get_audit_service,
)
from edgelite.services.backup_scheduler import (
    DatabaseBackupScheduler,
    BackupResult,
    ScheduledBackupStatus,
    get_backup_scheduler,
)

__all__ = [
    # Alarm
    "AlarmService",
    # Notification
    "NotificationManager",
    "get_notification_manager",
    "init_notification_manager",
    # i18n
    "I18n",
    "get_i18n",
    "set_language",
    "get_strings",
    "t",
    # Import/Export
    "DataExportService",
    "DataImportService",
    "TemplateService",
    "ExportFormat",
    "ImportMode",
    "DeviceTemplate",
    "RuleTemplate",
    "DeviceGroup",
    "get_export_service",
    "get_import_service",
    "get_template_service",
    # Historical Data
    "HistoricalDataService",
    "DeviceShadowService",
    "AggregationType",
    "QueryOptions",
    "get_historical_service",
    "get_shadow_service",
    # Command Approval
    "CommandApprovalService",
    "CommandRequest",
    "ApprovalChain",
    "ApprovalStatus",
    "get_approval_service",
    # System Services
    "ConfigBackupService",
    "LogRotationService",
    "ConfigAuditService",
    "ConfigValidator",
    "LogConfig",
    "BackupMetadata",
    "AuditEntry",
    "get_backup_service",
    "get_log_rotation_service",
    "get_audit_service",
    # Backup Scheduler
    "DatabaseBackupScheduler",
    "BackupResult",
    "ScheduledBackupStatus",
    "get_backup_scheduler",
]
