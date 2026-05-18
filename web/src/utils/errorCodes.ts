import { t } from '@/i18n'

const ERROR_CODE_MAP: Record<string, string> = {
  ERR_AUTH_RATE_LIMITED: 'login.rateLimited',
  ERR_AUTH_INVALID_CREDENTIALS: 'login.invalidCredentials',
  ERR_AUTH_USER_DISABLED: 'login.userDisabled',
  ERR_AUTH_LOGIN_FAILED: 'http.requestFailed',
  ERR_AUTH_REFRESH_TOKEN_INVALID: 'login.refreshTokenInvalid',
  ERR_AUTH_USER_NOT_FOUND: 'user.userNotFound',
  ERR_AUTH_OLD_PASSWORD_WRONG: 'login.oldPasswordWrong',
  ERR_AUTH_PASSWORD_POLICY: 'login.passwordPolicy',
  ERR_AUTH_PASSWORD_TOO_LONG: 'login.passwordTooLong',
  ERR_AUTH_PASSWORD_SAME_AS_OLD: 'login.passwordSameAsOld',
  ERR_AUTH_PASSWORD_LETTER_AND_DIGIT: 'login.passwordLetterAndDigit',
  ERR_AUTH_PASSWORD_CHANGE_FAILED: 'login.passwordChangeFailed',
  ERR_AUTH_TOKEN_INVALID: 'http.sessionExpired',
  ERR_AUTH_TOKEN_REVOKED: 'http.sessionExpired',
  ERR_AUTH_LOGOUT_FAILED: 'http.requestFailed',
  ERR_USER_LIST_FAILED: 'http.requestFailed',
  ERR_USER_USERNAME_EXISTS: 'user.usernameExists',
  ERR_USER_CREATE_FAILED: 'http.requestFailed',
  ERR_USER_CANNOT_REMOVE_LAST_ADMIN: 'user.cannotRemoveLastAdminRole',
  ERR_USER_USER_NOT_FOUND: 'user.userNotFound',
  ERR_USER_UPDATE_FAILED: 'http.requestFailed',
  ERR_USER_CANNOT_DELETE_SELF: 'user.cannotDeleteSelf',
  ERR_USER_CANNOT_DELETE_LAST_ADMIN: 'user.cannotDeleteLastAdmin',
  ERR_USER_DELETE_FAILED: 'http.requestFailed',
  ERR_SVC_LIST_FAILED: 'http.requestFailed',
  ERR_SVC_UNKNOWN_SERVICE: 'http.notFound',
  ERR_SVC_NOT_REGISTERED: 'http.notFound',
  ERR_SVC_STATUS_FAILED: 'http.requestFailed',
  ERR_SVC_ENABLE_FAILED: 'http.requestFailed',
  ERR_SVC_DISABLE_FAILED: 'http.requestFailed',
  ERR_SVC_START_FAILED: 'http.requestFailed',
  ERR_SVC_STOP_FAILED: 'http.requestFailed',
  ERR_SVC_INSTALL_FAILED: 'http.requestFailed',
  ERR_SVC_CONFIG_UPDATE_FAILED: 'http.requestFailed',
  ERR_SVC_DEPS_INSTALL_FAILED: 'http.requestFailed',
  ERR_SVC_SERIAL_PORT_UNAVAILABLE: 'device.serialPortUnavailable',
  ERR_DEVICE_NOT_FOUND: 'device.notFound',
  ERR_DEVICE_PUSH_EMPTY: 'device.pushEmpty',
  ERR_DEVICE_PUSH_INVALID_ID: 'device.pushInvalidId',
  ERR_DEVICE_WEBHOOK_AUTH_FAILED: 'device.webhookAuthFailed',
  ERR_DEVICE_PUSH_FAILED: 'http.requestFailed',
  ERR_DEVICE_LIST_FAILED: 'http.requestFailed',
  ERR_DEVICE_CREATE_FAILED: 'http.requestFailed',
  ERR_DEVICE_GET_FAILED: 'http.requestFailed',
  ERR_DEVICE_UPDATE_FAILED: 'http.requestFailed',
  ERR_DEVICE_DELETE_FAILED: 'http.requestFailed',
  ERR_DEVICE_POINTS_FAILED: 'http.requestFailed',
  ERR_DEVICE_WRITE_FAILED: 'http.requestFailed',
  ERR_DEVICE_SIMULATOR_FAILED: 'http.requestFailed',
  ERR_DEVICE_DISCOVER_FAILED: 'http.requestFailed',
  ERR_DEVICE_PUSH_DRIVER_NOT_READY: 'device.pushDriverNotReady',
  ERR_PREPROCESS_GET_FAILED: 'http.requestFailed',
  ERR_PREPROCESS_NOT_INITIALIZED: 'http.serviceNotReady',
  ERR_PREPROCESS_UPDATE_FAILED: 'http.requestFailed',
  ERR_COMMON_SERVICE_NOT_READY: 'http.serviceNotReady',
  ERR_COMMON_DB_NOT_READY: 'http.serviceNotReady',
  ERR_COMMON_INTERNAL_ERROR: 'http.requestFailed',
  ERR_COMMON_NOT_FOUND: 'http.notFound',
  ERR_GRAFANA_NOT_ENABLED: 'http.serviceNotReady',
  ERR_GRAFANA_BAD_STATUS: 'http.requestFailed',
  ERR_GRAFANA_DEPS_MISSING: 'http.serviceNotReady',
  ERR_GRAFANA_CONNECTION_FAILED: 'http.requestFailed',
  ERR_GRAFANA_INVALID_UID: 'http.requestFailed',
  ERR_OTA_NOT_ENABLED: 'http.serviceNotReady',
  ERR_OTA_CHECK_FAILED: 'http.requestFailed',
  ERR_OTA_IN_PROGRESS: 'http.requestFailed',
  ERR_OTA_NO_UPDATE: 'http.notFound',
  ERR_OTA_NO_DOWNLOAD_URL: 'http.requestFailed',
  ERR_OTA_DOWNLOAD_FAILED: 'http.requestFailed',
  ERR_OTA_APPLY_FAILED: 'http.requestFailed',
  ERR_OTA_ROLLBACK_FAILED: 'http.requestFailed',
  ERR_OTA_LIST_BACKUPS_FAILED: 'http.requestFailed',
  ERR_MCP_LIST_FAILED: 'http.requestFailed',
  ERR_MCP_CALL_FAILED: 'http.requestFailed',
  ERR_MCP_CREATE_KEY_FAILED: 'http.requestFailed',
  ERR_MCP_KEY_NOT_FOUND: 'http.notFound',
  ERR_MCP_SSE_FAILED: 'http.requestFailed',
  ERR_REPO_DEVICE_EXISTS: 'device.deviceExists',
  ERR_REPO_RULE_EXISTS: 'rule.ruleExists',
  ERR_REPO_ALARM_EXISTS: 'alarm.alarmExists',
  ERR_REPO_USERNAME_EXISTS: 'user.usernameExists',
  ERR_REPO_DB_MODE_SESSION_REQUIRED: 'http.requestFailed',
  ERR_REPO_NO_SESSION_AVAILABLE: 'http.requestFailed',
  ERR_DB_UNSUPPORTED_BACKEND: 'http.requestFailed',
  ERR_DB_DRIVER_REQUIRED: 'http.requestFailed',
  ERR_DB_NOT_CONNECTED: 'http.serviceNotReady',
  ERR_DB_SESSION_NOT_INIT: 'http.serviceNotReady',
  ERR_CONFIG_LOAD_FAILED: 'http.requestFailed',
  ERR_CONFIG_SAVE_FAILED: 'http.requestFailed',
  ERR_DRIVER_START_FAILED: 'http.requestFailed',
  ERR_DEVICE_API_KEY_INVALID: 'device.webhookAuthFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_DEVICE_API_KEY_NOT_CONFIGURED: 'device.webhookAuthFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_DRIVER_NOT_FOUND: 'http.notFound',  // FIXED: 原问题-缺失错误码映射
  ERR_DRIVER_REGISTRY_NOT_INIT: 'http.serviceNotReady',  // FIXED: 原问题-缺失错误码映射
  ERR_DRIVER_LIST_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_DRIVER_GET_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_DRIVER_DISCOVER_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_DATA_UNSUPPORTED_AGGREGATE: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_DATA_QUERY_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_DATA_EXPORT_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_VIDEO_PTZ_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_VIDEO_WEBHOOK_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_VIDEO_API_KEY_INVALID: 'device.webhookAuthFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_VIDEO_API_KEY_NOT_CONFIGURED: 'device.webhookAuthFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_AUDIT_INVALID_ACTION: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_AUDIT_INVALID_TIME_FORMAT: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_AUDIT_NOT_ENABLED: 'http.serviceNotReady',  // FIXED: 原问题-缺失错误码映射
  ERR_AUDIT_CLEANUP_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_AUDIT_EXPORT_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_AUDIT_INTEGRITY_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_AUDIT_LIST_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_EXPR_EVALUATE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_EXPR_BATCH_EVALUATE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_EXPR_VALIDATE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_SCADA_PROJECT_NOT_FOUND: 'http.notFound',  // FIXED: 原问题-缺失错误码映射
  ERR_SCADA_SAVE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_SCADA_DELETE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_SCADA_LOAD_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_PLATFORM_CONFIG_SCHEMA_NOT_FOUND: 'http.notFound',  // FIXED: 原问题-缺失错误码映射
  ERR_PLATFORM_CONNECT_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_PLATFORM_DISCONNECT_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_PLATFORM_NOT_SUPPORTED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_PLATFORM_MISSING_CONFIG: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_PLATFORM_NOT_CONNECTED: 'http.serviceNotReady',  // FIXED: 原问题-缺失错误码映射
  ERR_SYS_STATUS_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_SYS_BACKUP_LIST_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_SYS_BACKUP_CREATE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_SYS_INVALID_BACKUP_ID: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_SYS_BACKUP_NOT_FOUND: 'http.notFound',  // FIXED: 原问题-缺失错误码映射
  ERR_SYS_RESTORE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_RULE_LIST_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_RULE_CREATE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_RULE_NOT_FOUND: 'http.notFound',  // FIXED: 原问题-缺失错误码映射
  ERR_RULE_GET_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_RULE_UPDATE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_RULE_DELETE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_RULE_ENABLE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_RULE_DISABLE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_RULE_TEST_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_ALARM_LIST_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_ALARM_NOT_FOUND: 'http.notFound',  // FIXED: 原问题-缺失错误码映射
  ERR_ALARM_GET_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_ALARM_ACK_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_INTEG_HANDSHAKE_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_INTEG_STATUS_FAILED: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_MCP_DEVICE_SERVICE_UNAVAILABLE: 'http.serviceNotReady',  // FIXED: 原问题-缺失错误码映射
  ERR_MCP_MISSING_DEVICE_ID: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_MCP_DEVICE_NOT_FOUND: 'http.notFound',  // FIXED: 原问题-缺失错误码映射
  ERR_MCP_MISSING_PARAMS: 'http.requestFailed',  // FIXED: 原问题-缺失错误码映射
  ERR_MCP_ALARM_SERVICE_UNAVAILABLE: 'http.serviceNotReady',  // FIXED: 原问题-缺失错误码映射
  ERR_MCP_SYSTEM_SERVICE_UNAVAILABLE: 'http.serviceNotReady',  // FIXED: 原问题-缺失错误码映射
  ERR_MCP_RULE_SERVICE_UNAVAILABLE: 'http.serviceNotReady',  // FIXED: 原问题-缺失错误码映射
  ERR_MCP_UNKNOWN_TOOL: 'http.notFound',  // FIXED: 原问题-缺失错误码映射
  ERR_AUTHZ_NOT_AUTHENTICATED: 'http.sessionExpired',  // FIXED: 原问题-缺失错误码映射
  ERR_AUTHZ_PERMISSION_DENIED: 'http.forbidden',  // FIXED: 原问题-缺失错误码映射
}

// FIXED: 原问题-前端依赖后端中文detail字符串匹配错误类型，现改为基于错误码映射i18n
export function getErrorMessage(detail: string): string {
  if (!detail) return t('http.requestFailed')
  const i18nKey = ERROR_CODE_MAP[detail]
  if (i18nKey) return t(i18nKey)
  if (detail.startsWith('ERR_')) return t('http.requestFailed')
  if (detail.startsWith('ERR_GRAFANA_BAD_STATUS:')) return t('http.requestFailed')
  if (detail.startsWith('ERR_GRAFANA_CONNECTION_FAILED:')) return t('http.requestFailed')
  return detail
}
