# EdgeLite API 契约清单

> 本文档由 `scripts/check_api_contract.py` 与 `scripts/gen_api_contract_doc.py` 自动生成。
> 任何对 `src/edgelite/api/*.py` 或 `web/src/api/*.ts` 的修改都应同步更新本文档。
> CI 中可通过 `python scripts/check_api_contract.py` 自动校验前后端契约一致性。

---

## 1. 概览

| 指标 | 数值 |
|------|------|
| 后端 HTTP 路由数 | 379 |
| 后端 WebSocket 路由数 | 6 |
| 前端 HTTP 调用数 | 363 |
| 前端 WebSocket 调用数 | 5 |
| 404 风险（前端调用但后端无路由） | 25 |
| Dead Code 警告（后端有路由但前端未调用） | 49 |
| 未定义 URL 常量 | 0 |

**响应格式约定**：除特殊说明外，所有端点统一返回 `{code, message, data}` 结构，
其中 `code` 为业务错误码（0 表示成功），`message` 为可读消息，`data` 为业务数据。
分页端点返回 `{code, message, data, total, page, size}`。

**路径参数规范化**：本文档使用 `{var}` 表示路径参数位置。例如 `/api/v1/devices/{device_id}` 
在前端可能写作 `/api/v1/devices/123`、`/api/v1/devices/${id}` 或 `/api/v1/devices/${encodeURIComponent(id)}`。

---

## 2. 后端路由清单（按模块分组）

下表列出 `src/edgelite/api/*.py` 中所有 `@router.METHOD(...)` 装饰器定义的路由。

**列说明**：
- `Method` HTTP 方法
- `Path` 完整路径（已拼接 `APIRouter(prefix=...)`）
- `Function` 路由处理函数名
- `Response` `response_model`（如有）
- `Frontend` 前端是否调用：`[OK]` 已调用、`[WARN]` 未调用（potential dead code）

### 2.1 模块 `ai_models`

文件：`src/edgelite/api/ai_models.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/ai/ab-test` | `list_ab_tests` | `PagedResponse` | [WARN] |
| POST | `/api/v1/ai/ab-test` | `create_ab_test` | `ApiResponse` | [OK] |
| GET | `/api/v1/ai/ab-test/{test_id}` | `get_ab_test` | `ApiResponse` | [WARN] |
| POST | `/api/v1/ai/ab-test/{test_id}/promote` | `promote_ab_test` | `ApiResponse` | [WARN] |
| POST | `/api/v1/ai/ab-test/{test_id}/rollback` | `rollback_ab_test` | `ApiResponse` | [WARN] |
| POST | `/api/v1/ai/ab-test/{test_id}/split` | `update_ab_test_split` | `ApiResponse` | [WARN] |
| GET | `/api/v1/ai/batch/stats` | `get_batch_stats` | `ApiResponse` | [OK] |
| POST | `/api/v1/ai/cache/clear` | `clear_cache` | `ApiResponse` | [OK] |
| GET | `/api/v1/ai/cache/stats` | `get_cache_stats` | `ApiResponse` | [OK] |
| GET | `/api/v1/ai/devices` | `list_inference_devices` | `PagedResponse` | [OK] |
| GET | `/api/v1/ai/hot-swap` | `list_hot_swaps` | `PagedResponse` | [WARN] |
| POST | `/api/v1/ai/hot-swap` | `create_hot_swap` | `ApiResponse` | [OK] |
| POST | `/api/v1/ai/inference` | `inference` | `-` | [OK] |
| GET | `/api/v1/ai/inference/logs` | `get_inference_logs` | `-` | [OK] |
| GET | `/api/v1/ai/latency/{model_id}` | `get_latency_stats` | `ApiResponse` | [WARN] |
| GET | `/api/v1/ai/models` | `list_models` | `-` | [OK] |
| POST | `/api/v1/ai/models/upload` | `upload_model` | `-` | [OK] |
| GET | `/api/v1/ai/models/{model_id}` | `get_model` | `-` | [WARN] |
| PUT | `/api/v1/ai/models/{model_id}` | `update_model` | `-` | [WARN] |
| DELETE | `/api/v1/ai/models/{model_id}` | `delete_model` | `-` | [WARN] |
| POST | `/api/v1/ai/models/{model_id}/disable` | `disable_model` | `-` | [WARN] |
| POST | `/api/v1/ai/models/{model_id}/enable` | `enable_model` | `-` | [WARN] |
| GET | `/api/v1/ai/models/{model_id}/postprocess` | `get_postprocess` | `ApiResponse` | [WARN] |
| POST | `/api/v1/ai/models/{model_id}/postprocess` | `set_postprocess` | `ApiResponse` | [WARN] |
| GET | `/api/v1/ai/models/{model_id}/preprocess` | `get_preprocess` | `ApiResponse` | [WARN] |
| POST | `/api/v1/ai/models/{model_id}/preprocess` | `set_preprocess` | `ApiResponse` | [WARN] |
| POST | `/api/v1/ai/models/{model_id}/reload` | `reload_model` | `-` | [WARN] |
| POST | `/api/v1/ai/models/{model_id}/rollback` | `rollback_model` | `-` | [WARN] |
| POST | `/api/v1/ai/models/{model_id}/schedule` | `schedule_inference` | `ApiResponse` | [WARN] |
| DELETE | `/api/v1/ai/models/{model_id}/schedule` | `cancel_scheduled_inference` | `ApiResponse` | [WARN] |
| GET | `/api/v1/ai/models/{model_id}/stats` | `get_model_stats` | `-` | [WARN] |
| GET | `/api/v1/ai/models/{model_id}/versions` | `get_model_versions` | `-` | [WARN] |
| GET | `/api/v1/ai/postprocess/steps` | `list_postprocess_steps` | `ApiResponse` | [OK] |
| GET | `/api/v1/ai/preprocess/steps` | `list_preprocess_steps` | `ApiResponse` | [OK] |
| GET | `/api/v1/ai/resources` | `get_resources` | `ApiResponse` | [OK] |
| GET | `/api/v1/ai/schedules` | `list_scheduled_inferences` | `ApiResponse` | [OK] |
| GET | `/api/v1/ai/stats` | `get_stats` | `-` | [OK] |
| GET | `/api/v1/ai/summary` | `get_inference_summary` | `-` | [OK] |

### 2.2 模块 `alarms`

文件：`src/edgelite/api/alarms.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/alarms` | `list_alarms` | `PagedResponse[AlarmResponse]` | [OK] |
| POST | `/api/v1/alarms/batch-ack` | `batch_ack_alarms` | `ApiResponse` | [OK] |
| GET | `/api/v1/alarms/correlation` | `get_alarm_correlations` | `ApiResponse` | [OK] |
| GET | `/api/v1/alarms/history/{rule_id}` | `get_alarm_history` | `ApiResponse` | [WARN] |
| GET | `/api/v1/alarms/silence` | `list_alarm_silences` | `PagedResponse` | [OK] |
| POST | `/api/v1/alarms/silence` | `create_alarm_silence` | `ApiResponse` | [OK] |
| DELETE | `/api/v1/alarms/silence/{silence_id}` | `delete_alarm_silence` | `ApiResponse` | [WARN] |
| GET | `/api/v1/alarms/statistics` | `get_alarm_statistics` | `ApiResponse` | [OK] |
| GET | `/api/v1/alarms/trend` | `get_alarm_trend` | `ApiResponse` | [OK] |
| GET | `/api/v1/alarms/{alarm_id}` | `get_alarm` | `ApiResponse[AlarmResponse]` | [WARN] |
| DELETE | `/api/v1/alarms/{alarm_id}` | `delete_alarm` | `ApiResponse` | [WARN] |
| PUT | `/api/v1/alarms/{alarm_id}/ack` | `ack_alarm` | `ApiResponse[AlarmResponse]` | [WARN] |
| PUT | `/api/v1/alarms/{alarm_id}/recover` | `recover_alarm` | `ApiResponse[AlarmResponse]` | [WARN] |
| POST | `/api/v1/alarms/{alarm_id}/suppress` | `suppress_alarm` | `ApiResponse` | [WARN] |

### 2.3 模块 `anomaly_learner`

文件：`src/edgelite/api/anomaly_learner.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/anomaly-learner/dashboard` | `dashboard` | `ApiResponse` | [OK] |
| POST | `/api/v1/anomaly-learner/feedback` | `feedback` | `ApiResponse` | [OK] |
| POST | `/api/v1/anomaly-learner/infer` | `infer` | `ApiResponse` | [OK] |
| POST | `/api/v1/anomaly-learner/initialize` | `initialize` | `ApiResponse` | [OK] |
| GET | `/api/v1/anomaly-learner/status` | `status` | `ApiResponse` | [OK] |

### 2.4 模块 `audit`

文件：`src/edgelite/api/audit.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/audit/cleanup` | `cleanup_audit_logs` | `ApiResponse` | [OK] |
| GET | `/api/v1/audit/export/csv` | `export_audit_csv` | `ApiResponse` | [OK] |
| GET | `/api/v1/audit/integrity` | `verify_integrity` | `ApiResponse` | [OK] |
| GET | `/api/v1/audit/logs` | `query_audit_logs` | `PagedResponse` | [OK] |

### 2.5 模块 `auth`

文件：`src/edgelite/api/auth.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/auth/change-password` | `change_password` | `ApiResponse` | [OK] |
| POST | `/api/v1/auth/forgot-password` | `forgot_password` | `ApiResponse` | [WARN] |
| POST | `/api/v1/auth/login` | `login` | `ApiResponse[TokenResponse]` | [OK] |
| POST | `/api/v1/auth/logout` | `logout` | `ApiResponse` | [OK] |
| GET | `/api/v1/auth/me` | `get_current_user_info` | `ApiResponse[UserInfoResponse]` | [OK] |
| POST | `/api/v1/auth/refresh` | `refresh_token` | `ApiResponse[TokenResponse]` | [OK] |
| POST | `/api/v1/auth/reset-password` | `reset_password` | `ApiResponse` | [WARN] |

### 2.6 模块 `config_version`

文件：`src/edgelite/api/config_version.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/config/rollback/{version_id}` | `rollback_version` | `ApiResponse` | [WARN] |
| GET | `/api/v1/config/versions` | `list_versions` | `PagedResponse` | [OK] |
| GET | `/api/v1/config/versions/diff` | `diff_versions` | `ApiResponse` | [OK] |
| POST | `/api/v1/config/versions/snapshot` | `create_snapshot` | `ApiResponse` | [OK] |
| GET | `/api/v1/config/versions/{version_id}` | `get_version` | `ApiResponse` | [WARN] |

### 2.7 模块 `data`

文件：`src/edgelite/api/data.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/data/correlation` | `query_correlation` | `ApiResponse` | [OK] |
| POST | `/api/v1/data/downsample` | `downsample_timeseries` | `ApiResponse` | [OK] |
| GET | `/api/v1/data/export` | `export_data` | `-` | [OK] |
| POST | `/api/v1/data/export` | `export_config` | `ApiResponse` | [OK] |
| POST | `/api/v1/data/import` | `import_config` | `ApiResponse` | [OK] |
| GET | `/api/v1/data/multi-point` | `query_multi_point` | `ApiResponse` | [OK] |
| GET | `/api/v1/data/query` | `query_timeseries` | `ApiResponse` | [OK] |
| GET | `/api/v1/data/statistics` | `get_statistics` | `ApiResponse` | [OK] |
| GET | `/api/v1/data/stats` | `get_collect_stats` | `ApiResponse` | [OK] |
| GET | `/api/v1/data/trend` | `query_trend` | `ApiResponse` | [OK] |

### 2.8 模块 `data_quality`

文件：`src/edgelite/api/data_quality.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/data-quality/devices` | `list_devices` | `PagedResponse` | [OK] |
| GET | `/api/v1/data-quality/devices/{device_id}` | `get_device` | `ApiResponse` | [WARN] |
| GET | `/api/v1/data-quality/devices/{device_id}/points` | `list_points` | `ApiResponse` | [WARN] |
| GET | `/api/v1/data-quality/report` | `get_report` | `ApiResponse` | [OK] |
| POST | `/api/v1/data-quality/reset` | `reset` | `ApiResponse` | [OK] |
| GET | `/api/v1/data-quality/summary` | `get_summary` | `ApiResponse` | [OK] |
| GET | `/api/v1/data-quality/trend` | `get_trend` | `ApiResponse` | [OK] |

### 2.9 模块 `db_monitor`

文件：`src/edgelite/api/db_monitor.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/db-monitor/pool-stats` | `get_pool_stats` | `ApiResponse` | [OK] |
| GET | `/api/v1/db-monitor/slow-queries` | `get_slow_queries` | `ApiResponse` | [OK] |

### 2.10 模块 `debug`

文件：`src/edgelite/api/debug.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/debug/devices` | `list_debug_devices` | `ApiResponse` | [OK] |
| GET | `/api/v1/debug/packets` | `get_recent_packets` | `ApiResponse` | [OK] |
| DELETE | `/api/v1/debug/packets` | `clear_packets` | `ApiResponse` | [OK] |
| GET | `/api/v1/debug/protocols` | `list_debug_protocols` | `ApiResponse` | [OK] |
| POST | `/api/v1/debug/read` | `debug_read` | `ApiResponse` | [WARN] |
| POST | `/api/v1/debug/simulate` | `simulate_signal` | `ApiResponse` | [OK] |
| POST | `/api/v1/debug/write` | `debug_write` | `ApiResponse` | [WARN] |

### 2.11 模块 `device_linkage`

文件：`src/edgelite/api/device_linkage.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/linkage/executions` | `list_executions` | `PagedResponse` | [OK] |
| GET | `/api/v1/linkage/executions/stats` | `executions_stats` | `ApiResponse` | [OK] |
| GET | `/api/v1/linkage/rules` | `list_rules` | `PagedResponse` | [OK] |
| POST | `/api/v1/linkage/rules` | `create_rule` | `ApiResponse` | [OK] |
| GET | `/api/v1/linkage/rules/{rule_id}` | `get_rule` | `ApiResponse` | [WARN] |
| PUT | `/api/v1/linkage/rules/{rule_id}` | `update_rule` | `ApiResponse` | [WARN] |
| DELETE | `/api/v1/linkage/rules/{rule_id}` | `delete_rule` | `ApiResponse` | [WARN] |

### 2.12 模块 `devices`

文件：`src/edgelite/api/devices.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/devices` | `list_devices` | `PagedResponse[DeviceResponse]` | [OK] |
| POST | `/api/v1/devices` | `create_device` | `ApiResponse[DeviceResponse]` | [OK] |
| POST | `/api/v1/devices/batch-deploy` | `batch_deploy_config` | `ApiResponse[dict]` | [OK] |
| POST | `/api/v1/devices/batch/delete` | `batch_delete_devices` | `ApiResponse` | [WARN] |
| POST | `/api/v1/devices/batch/start-collect` | `batch_start_collect` | `ApiResponse` | [OK] |
| POST | `/api/v1/devices/batch/stop-collect` | `batch_stop_collect` | `ApiResponse` | [OK] |
| GET | `/api/v1/devices/collect-stats` | `get_collect_stats` | `ApiResponse` | [OK] |
| GET | `/api/v1/devices/device-quality-stats` | `get_device_quality_stats` | `ApiResponse` | [OK] |
| POST | `/api/v1/devices/discover` | `discover_devices` | `ApiResponse` | [OK] |
| POST | `/api/v1/devices/export` | `export_devices` | `ApiResponse` | [OK] |
| POST | `/api/v1/devices/from-template` | `create_from_template` | `ApiResponse[DeviceResponse]` | [OK] |
| GET | `/api/v1/devices/health` | `list_device_health_by_ids` | `ApiResponse` | [OK] |
| GET | `/api/v1/devices/health/all` | `list_all_device_health` | `ApiResponse` | [OK] |
| POST | `/api/v1/devices/import` | `import_devices` | `ApiResponse` | [OK] |
| POST | `/api/v1/devices/simulator` | `create_simulator` | `ApiResponse[DeviceResponse]` | [OK] |
| GET | `/api/v1/devices/templates` | `list_templates` | `ApiResponse[list[TemplateResponse]]` | [OK] |
| POST | `/api/v1/devices/templates` | `create_template` | `ApiResponse[TemplateResponse]` | [OK] |
| DELETE | `/api/v1/devices/templates/{name}` | `delete_template` | `ApiResponse` | [WARN] |
| POST | `/api/v1/devices/test-connection` | `test_device_connection` | `ApiResponse` | [OK] |
| GET | `/api/v1/devices/{device_id}` | `get_device` | `ApiResponse[DeviceResponse]` | [WARN] |
| PUT | `/api/v1/devices/{device_id}` | `update_device` | `ApiResponse[DeviceResponse]` | [WARN] |
| DELETE | `/api/v1/devices/{device_id}` | `delete_device` | `ApiResponse` | [WARN] |
| GET | `/api/v1/devices/{device_id}/config-versions` | `list_config_versions` | `ApiResponse` | [WARN] |
| POST | `/api/v1/devices/{device_id}/config-versions` | `save_config_version` | `ApiResponse` | [WARN] |
| GET | `/api/v1/devices/{device_id}/config-versions/audit` | `get_config_audit_trail` | `ApiResponse` | [WARN] |
| GET | `/api/v1/devices/{device_id}/config-versions/current` | `get_config_current` | `ApiResponse` | [WARN] |
| GET | `/api/v1/devices/{device_id}/config-versions/diff` | `diff_config_versions` | `ApiResponse` | [WARN] |
| POST | `/api/v1/devices/{device_id}/config-versions/rollback` | `rollback_config_version` | `ApiResponse` | [WARN] |
| GET | `/api/v1/devices/{device_id}/config-versions/{version}` | `get_config_version_detail` | `ApiResponse` | [WARN] |
| GET | `/api/v1/devices/{device_id}/health` | `get_device_health` | `ApiResponse[DeviceHealthResponse]` | [WARN] |
| POST | `/api/v1/devices/{device_id}/health/reset` | `reset_device_health` | `ApiResponse` | [WARN] |
| GET | `/api/v1/devices/{device_id}/metrics` | `get_device_metrics` | `ApiResponse` | [WARN] |
| GET | `/api/v1/devices/{device_id}/ops` | `get_device_ops` | `ApiResponse` | [WARN] |
| GET | `/api/v1/devices/{device_id}/point-health` | `get_point_health` | `ApiResponse` | [WARN] |
| GET | `/api/v1/devices/{device_id}/points` | `get_device_points` | `ApiResponse` | [WARN] |
| POST | `/api/v1/devices/{device_id}/points` | `write_device_point` | `ApiResponse` | [WARN] |
| POST | `/api/v1/devices/{device_id}/probe-primary` | `probe_primary_link` | `ApiResponse` | [WARN] |
| POST | `/api/v1/devices/{device_id}/push` | `push_device_data` | `ApiResponse` | [WARN] |
| GET | `/api/v1/devices/{device_id}/write-audit` | `get_write_audit` | `ApiResponse` | [WARN] |
| PUT | `/api/v1/devices/{device_id}/write-policy` | `update_write_policy` | `ApiResponse[DeviceResponse]` | [WARN] |

### 2.13 模块 `drivers`

文件：`src/edgelite/api/drivers.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/drivers` | `list_all_drivers` | `ApiResponse[list[DriverStatusInfo]]` | [OK] |
| GET | `/api/v1/drivers/health` | `get_all_drivers_health` | `ApiResponse` | [WARN] |
| GET | `/api/v1/drivers/list` | `list_drivers` | `ApiResponse[DriverListResponse]` | [OK] |
| GET | `/api/v1/drivers/load-status` | `get_driver_load_status` | `ApiResponse[dict]` | [OK] |
| GET | `/api/v1/drivers/meta` | `list_driver_meta` | `ApiResponse` | [OK] |
| GET | `/api/v1/drivers/opc-da/servers` | `list_opc_da_servers` | `ApiResponse` | [OK] |
| POST | `/api/v1/drivers/opcua/browse` | `browse_opcua_nodes` | `ApiResponse[list[dict]]` | [OK] |
| GET | `/api/v1/drivers/opcua/certificate-status` | `get_opcua_certificate_status` | `ApiResponse` | [WARN] |
| GET | `/api/v1/drivers/protocols` | `list_protocols` | `ApiResponse[DriverProtocolsResponse]` | [OK] |
| GET | `/api/v1/drivers/{driver_name}/config-schema` | `get_driver_config_schema` | `ApiResponse[DriverConfigSchemaResponse]` | [WARN] |
| POST | `/api/v1/drivers/{driver_name}/discover` | `discover_devices` | `ApiResponse[DriverDiscoverResponse]` | [WARN] |
| GET | `/api/v1/drivers/{driver_name}/environment-check` | `driver_environment_check` | `ApiResponse` | [WARN] |

### 2.14 模块 `expressions`

文件：`src/edgelite/api/expressions.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/expressions/evaluate` | `evaluate_expression` | `ApiResponse` | [OK] |
| POST | `/api/v1/expressions/evaluate-batch` | `evaluate_batch` | `ApiResponse` | [OK] |
| GET | `/api/v1/expressions/functions` | `list_available_functions` | `ApiResponse` | [OK] |
| POST | `/api/v1/expressions/validate` | `validate_expression` | `ApiResponse` | [OK] |

### 2.15 模块 `firmware_signature`

文件：`src/edgelite/api/firmware_signature.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/firmware/manifest/generate` | `generate_manifest` | `ApiResponse` | [OK] |
| POST | `/api/v1/firmware/verify/hash` | `verify_hash` | `ApiResponse` | [OK] |
| POST | `/api/v1/firmware/verify/signature` | `verify_signature` | `ApiResponse` | [OK] |

### 2.16 模块 `grafana`

文件：`src/edgelite/api/grafana.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/grafana/config` | `get_grafana_config` | `ApiResponse` | [OK] |
| GET | `/api/v1/grafana/dashboards` | `list_grafana_dashboards` | `ApiResponse` | [OK] |
| GET | `/api/v1/grafana/embed-url` | `get_grafana_embed_url` | `ApiResponse` | [OK] |

### 2.17 模块 `health`

文件：`src/edgelite/api/health.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/health` | `health_full` | `-` | [WARN] |
| GET | `/health/live` | `health_live` | `-` | [WARN] |
| GET | `/health/ready` | `health_ready` | `-` | [WARN] |
| GET | `/live` | `health_live_alias` | `-` | [WARN] |
| GET | `/ready` | `health_ready_alias` | `-` | [WARN] |

### 2.18 模块 `integration`

文件：`src/edgelite/api/integration.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/integration/handshake` | `handshake` | `ApiResponse` | [OK] |
| GET | `/api/v1/integration/health` | `integration_health_check` | `ApiResponse` | [WARN] |
| POST | `/api/v1/integration/push-device` | `push_device` | `ApiResponse` | [WARN] |
| POST | `/api/v1/integration/rpc/execute` | `execute_rpc_command` | `ApiResponse[RpcExecuteResponse]` | [OK] |
| GET | `/api/v1/integration/rpc/history` | `get_rpc_history` | `ApiResponse` | [OK] |
| GET | `/api/v1/integration/status` | `get_integration_status` | `ApiResponse` | [OK] |

### 2.19 模块 `log_aggregation`

文件：`src/edgelite/api/log_aggregation.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/logs/archive` | `archive_logs` | `ApiResponse` | [OK] |
| POST | `/api/v1/logs/cleanup` | `cleanup_logs` | `ApiResponse` | [OK] |
| GET | `/api/v1/logs/filters` | `get_filters` | `ApiResponse` | [WARN] |
| PUT | `/api/v1/logs/level` | `set_log_level` | `ApiResponse` | [WARN] |
| GET | `/api/v1/logs/query` | `query_logs` | `PagedResponse` | [OK] |
| GET | `/api/v1/logs/stats` | `get_log_stats` | `ApiResponse` | [OK] |

### 2.20 模块 `mcp`

文件：`src/edgelite/api/mcp.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/mcp/auth-keys` | `list_auth_keys` | `ApiResponse` | [OK] |
| POST | `/api/v1/mcp/auth-keys` | `create_auth_key` | `ApiResponse` | [OK] |
| DELETE | `/api/v1/mcp/auth-keys/{key_id}` | `delete_auth_key` | `ApiResponse` | [WARN] |
| POST | `/api/v1/mcp/call` | `call_tool` | `ApiResponse` | [OK] |
| GET | `/api/v1/mcp/prompts` | `list_prompts` | `ApiResponse` | [OK] |
| GET | `/api/v1/mcp/resources` | `list_resources` | `ApiResponse` | [OK] |
| GET | `/api/v1/mcp/sse` | `mcp_sse` | `-` | [WARN] |
| GET | `/api/v1/mcp/sse-ticket` | `create_sse_ticket` | `ApiResponse` | [WARN] |
| GET | `/api/v1/mcp/tools` | `list_tools` | `ApiResponse` | [OK] |

### 2.21 模块 `metrics`

文件：`src/edgelite/api/metrics.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/metrics` | `prometheus_metrics` | `-` | [WARN] |
| GET | `/api/v1/metrics.json` | `prometheus_metrics_json` | `-` | [WARN] |
| GET | `/metrics` | `root_prometheus_metrics` | `-` | [WARN] |

### 2.22 模块 `modbus_slave`

文件：`src/edgelite/api/modbus_slave.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| PUT | `/api/v1/modbus-slave/config` | `update_modbus_slave_config` | `ApiResponse` | [OK] |
| GET | `/api/v1/modbus-slave/devices/{device_id}/ops` | `get_slave_ops` | `ApiResponse` | [WARN] |
| POST | `/api/v1/modbus-slave/start` | `start_modbus_slave` | `ApiResponse` | [OK] |
| GET | `/api/v1/modbus-slave/status` | `get_modbus_slave_status` | `ApiResponse` | [OK] |
| POST | `/api/v1/modbus-slave/stop` | `stop_modbus_slave` | `ApiResponse` | [OK] |

### 2.23 模块 `mqtt_forwarder`

文件：`src/edgelite/api/mqtt_forwarder.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/mqtt-forwarder/offline-queue/status` | `get_offline_queue_status` | `-` | [OK] |

### 2.24 模块 `mqtt_server`

文件：`src/edgelite/api/mqtt_server.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| PUT | `/api/v1/mqtt-server/config` | `update_mqtt_server_config` | `ApiResponse` | [OK] |
| POST | `/api/v1/mqtt-server/start` | `start_mqtt_server` | `ApiResponse` | [OK] |
| GET | `/api/v1/mqtt-server/status` | `get_mqtt_server_status` | `ApiResponse` | [OK] |
| POST | `/api/v1/mqtt-server/stop` | `stop_mqtt_server` | `ApiResponse` | [OK] |

### 2.25 模块 `notify`

文件：`src/edgelite/api/notify.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/notify/channels` | `list_channels` | `ApiResponse` | [OK] |
| POST | `/api/v1/notify/channels/dingtalk` | `update_dingtalk` | `ApiResponse` | [OK] |
| POST | `/api/v1/notify/channels/email` | `update_email` | `ApiResponse` | [OK] |
| POST | `/api/v1/notify/channels/webhook` | `update_webhook` | `ApiResponse` | [OK] |
| POST | `/api/v1/notify/channels/wecom` | `update_wecom` | `ApiResponse` | [OK] |
| DELETE | `/api/v1/notify/channels/{channel_id}` | `delete_channel` | `ApiResponse` | [WARN] |
| POST | `/api/v1/notify/channels/{channel_id}/enable` | `enable_channel` | `ApiResponse` | [WARN] |
| POST | `/api/v1/notify/channels/{channel_id}/test` | `test_channel` | `ApiResponse` | [WARN] |

### 2.26 模块 `observability`

文件：`src/edgelite/api/observability.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/observability/alerts/events` | `list_alert_events` | `ApiResponse` | [OK] |
| POST | `/api/v1/observability/alerts/events/{name}/{ts}/resolve` | `resolve_alert_event` | `ApiResponse` | [WARN] |
| GET | `/api/v1/observability/alerts/rules` | `list_alert_rules` | `ApiResponse` | [OK] |
| GET | `/api/v1/observability/latency` | `get_latency` | `ApiResponse` | [OK] |
| GET | `/api/v1/observability/latency/histogram` | `get_latency_histogram` | `ApiResponse` | [OK] |
| GET | `/api/v1/observability/latency/percentiles` | `get_latency_percentiles` | `ApiResponse` | [OK] |
| GET | `/api/v1/observability/metrics` | `get_metrics` | `ApiResponse` | [OK] |
| GET | `/api/v1/observability/overview` | `get_overview` | `ApiResponse` | [OK] |
| GET | `/api/v1/observability/traces` | `list_traces` | `PagedResponse` | [OK] |
| GET | `/api/v1/observability/traces/stats/{node}` | `get_trace_stats` | `ApiResponse` | [WARN] |
| GET | `/api/v1/observability/traces/{trace_id}` | `get_trace` | `ApiResponse` | [WARN] |

### 2.27 模块 `ota`

文件：`src/edgelite/api/ota.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/ota/apply` | `apply_update` | `ApiResponse` | [OK] |
| GET | `/api/v1/ota/backups` | `list_ota_backups` | `ApiResponse` | [OK] |
| POST | `/api/v1/ota/cancel` | `cancel_ota` | `ApiResponse` | [OK] |
| GET | `/api/v1/ota/check` | `check_update` | `ApiResponse` | [OK] |
| POST | `/api/v1/ota/rollback` | `rollback_update` | `ApiResponse` | [OK] |
| GET | `/api/v1/ota/status` | `get_ota_status` | `ApiResponse` | [OK] |

### 2.28 模块 `platforms`

文件：`src/edgelite/api/platforms.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/platforms/alarm-records/{platform_name}` | `get_platform_alarm_records` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/broker-quality/{platform_name}` | `get_broker_quality` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/broker-status/{platform_name}` | `get_broker_status` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/command-logs/{platform_name}` | `get_platform_command_logs` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/config-schema/{platform_name}` | `get_platform_config_schema` | `ApiResponse` | [WARN] |
| POST | `/api/v1/platforms/connect/{platform_name}` | `connect_platform` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/dashboard` | `get_platform_dashboard` | `ApiResponse` | [OK] |
| GET | `/api/v1/platforms/device-mapping/{platform_name}` | `get_platform_device_mapping` | `ApiResponse` | [WARN] |
| POST | `/api/v1/platforms/disconnect/{platform_name}` | `disconnect_platform` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/export/{platform_name}` | `export_platform_config` | `ApiResponse` | [WARN] |
| POST | `/api/v1/platforms/import/{platform_name}` | `import_platform_config` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/list` | `list_platforms` | `ApiResponse` | [OK] |
| GET | `/api/v1/platforms/message-preview/{platform_name}` | `get_message_preview` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/metrics` | `get_north_metrics` | `-` | [OK] |
| POST | `/api/v1/platforms/mqtt-test-publish/{platform_name}` | `mqtt_test_publish` | `ApiResponse` | [WARN] |
| POST | `/api/v1/platforms/preview-template` | `preview_template` | `ApiResponse` | [OK] |
| POST | `/api/v1/platforms/reload/{platform_name}` | `reload_platform_config` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/shadow/{platform_name}` | `get_platform_shadow` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/status/{platform_name}` | `get_platform_status` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/tb/alarms/{platform_name}` | `get_tb_alarm_records` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/tb/devices/{platform_name}` | `get_tb_devices` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/tb/rpc-logs/{platform_name}` | `get_tb_rpc_logs` | `ApiResponse` | [WARN] |
| GET | `/api/v1/platforms/tb/sync-status/{platform_name}` | `get_tb_sync_status` | `ApiResponse` | [WARN] |
| POST | `/api/v1/platforms/test-connection/{platform_name}` | `test_connection` | `ApiResponse` | [WARN] |
| POST | `/api/v1/platforms/test-script` | `test_script` | `ApiResponse` | [OK] |
| POST | `/api/v1/platforms/validate-advanced-template` | `validate_advanced_template` | `ApiResponse` | [OK] |
| POST | `/api/v1/platforms/validate-script` | `validate_script` | `ApiResponse` | [OK] |
| POST | `/api/v1/platforms/validate-topic` | `validate_topic_template` | `ApiResponse` | [OK] |

### 2.29 模块 `preprocess`

文件：`src/edgelite/api/preprocess.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/preprocess/config` | `get_preprocess_config` | `ApiResponse[PreprocessConfigResponse]` | [OK] |
| PUT | `/api/v1/preprocess/config` | `update_preprocess_config` | `ApiResponse` | [OK] |

### 2.30 模块 `profiler`

文件：`src/edgelite/api/profiler.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/profiler/disable` | `disable_profiler` | `ApiResponse` | [OK] |
| POST | `/api/v1/profiler/enable` | `enable_profiler` | `ApiResponse` | [OK] |
| GET | `/api/v1/profiler/export` | `export_profiler` | `ApiResponse` | [OK] |
| GET | `/api/v1/profiler/memory` | `get_memory` | `ApiResponse` | [OK] |
| GET | `/api/v1/profiler/requests` | `list_requests` | `PagedResponse` | [OK] |
| POST | `/api/v1/profiler/reset` | `reset_profiler` | `ApiResponse` | [OK] |
| GET | `/api/v1/profiler/slowest` | `get_slowest` | `ApiResponse` | [OK] |
| GET | `/api/v1/profiler/stats` | `get_stats` | `ApiResponse` | [OK] |

### 2.31 模块 `protocol_bridge`

文件：`src/edgelite/api/protocol_bridge.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/bridge/create` | `create_bridge` | `ApiResponse` | [OK] |
| GET | `/api/v1/bridge/list` | `list_bridges` | `PagedResponse` | [OK] |
| GET | `/api/v1/bridge/{name}` | `get_bridge` | `ApiResponse` | [WARN] |
| PUT | `/api/v1/bridge/{name}` | `update_bridge` | `ApiResponse` | [WARN] |
| POST | `/api/v1/bridge/{name}/disable` | `disable_bridge` | `ApiResponse` | [WARN] |
| POST | `/api/v1/bridge/{name}/enable` | `enable_bridge` | `ApiResponse` | [WARN] |

### 2.32 模块 `resource_shares`

文件：`src/edgelite/api/resource_shares.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/resource-shares` | `list_my_shares` | `ApiResponse` | [WARN] |
| POST | `/api/v1/resource-shares` | `share_resource` | `ApiResponse` | [OK] |
| DELETE | `/api/v1/resource-shares` | `unshare_resource` | `ApiResponse` | [OK] |
| POST | `/api/v1/resource-shares/check` | `check_access` | `ApiResponse` | [WARN] |
| GET | `/api/v1/resource-shares/resource/{resource_type}/{resource_id}` | `list_shares_for_resource` | `ApiResponse` | [WARN] |

### 2.33 模块 `rules`

文件：`src/edgelite/api/rules.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/rules` | `list_rules` | `PagedResponse[RuleResponse]` | [OK] |
| POST | `/api/v1/rules` | `create_rule` | `ApiResponse[RuleResponse]` | [OK] |
| POST | `/api/v1/rules/batch/delete` | `batch_delete_rules` | `ApiResponse` | [WARN] |
| POST | `/api/v1/rules/batch/disable` | `batch_disable_rules` | `ApiResponse` | [WARN] |
| POST | `/api/v1/rules/batch/enable` | `batch_enable_rules` | `ApiResponse` | [WARN] |
| POST | `/api/v1/rules/test` | `test_rule_definition` | `ApiResponse` | [WARN] |
| GET | `/api/v1/rules/{rule_id}` | `get_rule` | `ApiResponse[RuleResponse]` | [WARN] |
| PUT | `/api/v1/rules/{rule_id}` | `update_rule` | `ApiResponse[RuleResponse]` | [WARN] |
| DELETE | `/api/v1/rules/{rule_id}` | `delete_rule` | `ApiResponse` | [WARN] |
| POST | `/api/v1/rules/{rule_id}/disable` | `disable_rule` | `ApiResponse[RuleResponse]` | [WARN] |
| POST | `/api/v1/rules/{rule_id}/enable` | `enable_rule` | `ApiResponse[RuleResponse]` | [WARN] |
| POST | `/api/v1/rules/{rule_id}/test` | `test_rule` | `ApiResponse` | [WARN] |

### 2.34 模块 `scada`

文件：`src/edgelite/api/scada.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/scada/project` | `save_project` | `ApiResponse` | [OK] |
| GET | `/api/v1/scada/project/{name}` | `get_project` | `ApiResponse` | [WARN] |
| DELETE | `/api/v1/scada/project/{name}` | `delete_project` | `ApiResponse` | [WARN] |
| GET | `/api/v1/scada/projects` | `list_projects` | `ApiResponse` | [OK] |

### 2.35 模块 `scripts`

文件：`src/edgelite/api/scripts.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/scripts/create` | `create_script` | `ApiResponse` | [OK] |
| GET | `/api/v1/scripts/list` | `list_scripts` | `PagedResponse` | [OK] |
| GET | `/api/v1/scripts/{script_id}` | `get_script` | `ApiResponse` | [WARN] |
| PUT | `/api/v1/scripts/{script_id}` | `update_script` | `ApiResponse` | [WARN] |
| POST | `/api/v1/scripts/{script_id}/approve` | `approve_script` | `ApiResponse` | [WARN] |
| POST | `/api/v1/scripts/{script_id}/disable` | `disable_script` | `ApiResponse` | [WARN] |
| POST | `/api/v1/scripts/{script_id}/enable` | `enable_script` | `ApiResponse` | [WARN] |
| GET | `/api/v1/scripts/{script_id}/logs` | `list_script_logs` | `PagedResponse` | [WARN] |
| POST | `/api/v1/scripts/{script_id}/reject` | `reject_script` | `ApiResponse` | [WARN] |
| POST | `/api/v1/scripts/{script_id}/submit-review` | `submit_review` | `ApiResponse` | [WARN] |
| POST | `/api/v1/scripts/{script_id}/test` | `test_script` | `ApiResponse` | [WARN] |

### 2.36 模块 `serial_bridge`

文件：`src/edgelite/api/serial_bridge.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| PUT | `/api/v1/serial-bridge/config` | `update_serial_bridge_config` | `ApiResponse` | [OK] |
| POST | `/api/v1/serial-bridge/start` | `start_serial_bridge` | `ApiResponse` | [OK] |
| GET | `/api/v1/serial-bridge/status` | `get_serial_bridge_status` | `ApiResponse[SerialBridgeStatusResponse]` | [OK] |
| POST | `/api/v1/serial-bridge/stop` | `stop_serial_bridge` | `ApiResponse` | [OK] |

### 2.37 模块 `services`

文件：`src/edgelite/api/services.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/services/list` | `list_services` | `ApiResponse` | [OK] |
| PUT | `/api/v1/services/{service_name}/config` | `update_service_config` | `ApiResponse` | [WARN] |
| POST | `/api/v1/services/{service_name}/disable` | `disable_service` | `ApiResponse` | [WARN] |
| POST | `/api/v1/services/{service_name}/enable` | `enable_service` | `ApiResponse` | [WARN] |
| POST | `/api/v1/services/{service_name}/install-deps` | `install_service_dependencies` | `ApiResponse` | [WARN] |
| POST | `/api/v1/services/{service_name}/start` | `start_service` | `ApiResponse` | [WARN] |
| GET | `/api/v1/services/{service_name}/status` | `get_service_status` | `ApiResponse` | [WARN] |
| POST | `/api/v1/services/{service_name}/stop` | `stop_service` | `ApiResponse` | [WARN] |

### 2.38 模块 `shadow`

文件：`src/edgelite/api/shadow.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/shadows` | `list_shadows` | `ApiResponse` | [OK] |
| GET | `/api/v1/shadows/{device_id}` | `get_shadow` | `ApiResponse` | [WARN] |
| GET | `/api/v1/shadows/{device_id}/delta` | `get_delta` | `ApiResponse` | [WARN] |
| PUT | `/api/v1/shadows/{device_id}/desired` | `update_desired` | `ApiResponse` | [WARN] |
| POST | `/api/v1/shadows/{device_id}/reported` | `update_reported` | `ApiResponse` | [WARN] |

### 2.39 模块 `simulation`

文件：`src/edgelite/api/simulation.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/simulation/assess` | `assess` | `ApiResponse` | [OK] |
| POST | `/api/v1/simulation/export` | `export` | `ApiResponse` | [OK] |
| POST | `/api/v1/simulation/preview` | `preview` | `ApiResponse` | [OK] |
| POST | `/api/v1/simulation/run` | `run` | `ApiResponse` | [OK] |
| GET | `/api/v1/simulation/types` | `list_types` | `ApiResponse` | [OK] |

### 2.40 模块 `system`

文件：`src/edgelite/api/system.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/system/backup` | `list_backups` | `ApiResponse` | [OK] |
| POST | `/api/v1/system/backup` | `create_backup` | `ApiResponse` | [OK] |
| GET | `/api/v1/system/backup/schedule` | `get_backup_schedule` | `ApiResponse` | [OK] |
| POST | `/api/v1/system/backup/schedule/trigger` | `trigger_backup` | `ApiResponse` | [OK] |
| DELETE | `/api/v1/system/backup/{backup_id}` | `delete_backup` | `ApiResponse` | [WARN] |
| POST | `/api/v1/system/cascade/config` | `update_cascade_config` | `ApiResponse` | [OK] |
| GET | `/api/v1/system/cascade/neighbors` | `get_cascade_neighbors` | `ApiResponse` | [OK] |
| DELETE | `/api/v1/system/cascade/neighbors/{neighbor_id}` | `remove_cascade_neighbor` | `ApiResponse` | [WARN] |
| GET | `/api/v1/system/cascade/topology` | `get_cascade_topology` | `ApiResponse` | [OK] |
| GET | `/api/v1/system/cert` | `get_cert_info` | `ApiResponse[dict]` | [OK] |
| POST | `/api/v1/system/cert/rotate` | `rotate_cert` | `ApiResponse[dict]` | [OK] |
| GET | `/api/v1/system/circuit-breakers` | `get_circuit_breaker_status` | `ApiResponse` | [WARN] |
| POST | `/api/v1/system/circuit-breakers/{device_id}/reset` | `reset_circuit_breaker` | `ApiResponse` | [WARN] |
| GET | `/api/v1/system/config` | `get_current_config` | `ApiResponse` | [OK] |
| POST | `/api/v1/system/config/reload` | `reload_config` | `ApiResponse` | [OK] |
| PUT | `/api/v1/system/config/{section}` | `update_config_section` | `ApiResponse` | [WARN] |
| GET | `/api/v1/system/health/basic` | `health_check_basic` | `ApiResponse[dict]` | [WARN] |
| GET | `/api/v1/system/locks/status` | `get_lock_status` | `ApiResponse` | [WARN] |
| GET | `/api/v1/system/migration/history` | `get_migration_history` | `ApiResponse` | [WARN] |
| POST | `/api/v1/system/migration/retry` | `retry_migration` | `ApiResponse` | [WARN] |
| GET | `/api/v1/system/migration/status` | `get_migration_status` | `ApiResponse` | [WARN] |
| GET | `/api/v1/system/network` | `get_network_info` | `ApiResponse` | [OK] |
| GET | `/api/v1/system/ntp` | `get_ntp_config` | `ApiResponse[dict]` | [OK] |
| PUT | `/api/v1/system/ntp` | `update_ntp_config` | `ApiResponse[dict]` | [OK] |
| GET | `/api/v1/system/performance` | `get_performance` | `ApiResponse[dict]` | [OK] |
| GET | `/api/v1/system/quality/{device_id}` | `get_device_quality` | `ApiResponse` | [WARN] |
| GET | `/api/v1/system/ready-status` | `readiness_check_api` | `ApiResponse[dict]` | [WARN] |
| GET | `/api/v1/system/resources` | `get_system_resources` | `ApiResponse[SystemResourcesResponse]` | [OK] |
| POST | `/api/v1/system/restore` | `restore_backup` | `ApiResponse` | [OK] |
| GET | `/api/v1/system/retention` | `get_retention_policy` | `ApiResponse[dict]` | [OK] |
| PUT | `/api/v1/system/retention` | `update_retention_policy` | `ApiResponse[dict]` | [OK] |
| GET | `/api/v1/system/status` | `get_system_status` | `ApiResponse[SystemStatusResponse]` | [OK] |

### 2.41 模块 `threshold_learner`

文件：`src/edgelite/api/threshold_learner.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/threshold-learner/dashboard` | `dashboard` | `ApiResponse` | [OK] |
| GET | `/api/v1/threshold-learner/decomposition` | `decomposition` | `ApiResponse` | [OK] |
| POST | `/api/v1/threshold-learner/feedback` | `feedback` | `ApiResponse` | [OK] |
| POST | `/api/v1/threshold-learner/infer` | `infer` | `ApiResponse` | [OK] |
| POST | `/api/v1/threshold-learner/initialize` | `initialize` | `ApiResponse` | [OK] |

### 2.42 模块 `trend_learner`

文件：`src/edgelite/api/trend_learner.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/trend-learner/dashboard` | `dashboard` | `ApiResponse` | [OK] |
| POST | `/api/v1/trend-learner/initialize` | `initialize` | `ApiResponse` | [OK] |
| POST | `/api/v1/trend-learner/predict` | `predict` | `ApiResponse` | [OK] |
| GET | `/api/v1/trend-learner/residual-analysis` | `residual_analysis` | `ApiResponse` | [OK] |

### 2.43 模块 `users`

文件：`src/edgelite/api/users.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| GET | `/api/v1/users` | `list_users` | `PagedResponse[UserResponse]` | [OK] |
| POST | `/api/v1/users` | `create_user` | `ApiResponse[UserResponse]` | [OK] |
| GET | `/api/v1/users/{user_id}` | `get_user` | `ApiResponse[UserResponse]` | [WARN] |
| PUT | `/api/v1/users/{user_id}` | `update_user` | `ApiResponse[UserResponse]` | [WARN] |
| DELETE | `/api/v1/users/{user_id}` | `delete_user` | `ApiResponse` | [WARN] |

### 2.44 模块 `video`

文件：`src/edgelite/api/video.py`

| Method | Path | Function | Response | Frontend |
|--------|------|----------|----------|----------|
| POST | `/api/v1/video/webhook` | `video_webhook` | `ApiResponse` | [OK] |
| POST | `/api/v1/video/{device_id}/ptz` | `ptz_control` | `ApiResponse` | [WARN] |
| GET | `/api/v1/video/{device_id}/stream` | `get_stream_url` | `ApiResponse` | [WARN] |

---

## 3. 前端调用清单（按 api_name 分组）

下表列出 `web/src/api/*.ts` 中所有 `http.METHOD(...)` 调用（含 POLL_API_MAP 轮询降级路径）。
URL 常量引用（如 `URL.OTA.CHECK`）已通过常量传播解析为完整路径。

**列说明**：
- `Method` HTTP 方法
- `Path` 完整路径（已拼接 baseURL `/api/v1`）
- `Raw URL` 前端源码中的 URL 表达式
- `Source` 调用位置（文件:行号）
- `Backend` 后端是否实现：`[OK]` 已实现、`[FAIL]` 未实现（404 风险）

### 3.1 API ``

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/auth/refresh` | `'/auth/refresh'` | `web/src/api/http.ts:108` | [OK] |

### 3.2 API `POLL_API_MAP`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/ai/stats` | `/api/v1/ai/stats` | `web/src/api/websocket.ts:8` | [OK] |
| GET | `/api/v1/alarms` | `/api/v1/alarms?size=20&page=1&status=firing` | `web/src/api/websocket.ts:2` | [OK] |
| GET | `/api/v1/data/stats` | `/api/v1/data/stats` | `web/src/api/websocket.ts:8` | [OK] |
| GET | `/api/v1/devices/health/all` | `/api/v1/devices/health/all` | `web/src/api/websocket.ts:6` | [OK] |
| GET | `/api/v1/system/status` | `/api/v1/system/status` | `web/src/api/websocket.ts:9` | [OK] |

### 3.3 API `aiApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/ai/inference` | `'/ai/inference'` | `web/src/api/index.ts:1174` | [OK] |
| GET | `/api/v1/ai/inference/logs` | `'/ai/inference/logs'` | `web/src/api/index.ts:1182` | [OK] |
| GET | `/api/v1/ai/models` | `'/ai/models'` | `web/src/api/index.ts:1164` | [OK] |
| POST | `/api/v1/ai/models/upload` | `'/ai/models/upload'` | `web/src/api/index.ts:1199` | [OK] |
| GET | `/api/v1/ai/models/{var}` | `\`/ai/models/${id}\`` | `web/src/api/index.ts:1166` | [FAIL] |
| PUT | `/api/v1/ai/models/{var}` | `\`/ai/models/${id}\`` | `web/src/api/index.ts:1184` | [FAIL] |
| DELETE | `/api/v1/ai/models/{var}` | `\`/ai/models/${id}\`` | `web/src/api/index.ts:1186` | [FAIL] |
| POST | `/api/v1/ai/models/{var}/disable` | `\`/ai/models/${id}/disable\`` | `web/src/api/index.ts:1170` | [FAIL] |
| POST | `/api/v1/ai/models/{var}/enable` | `\`/ai/models/${id}/enable\`` | `web/src/api/index.ts:1168` | [FAIL] |
| POST | `/api/v1/ai/models/{var}/reload` | `\`/ai/models/${id}/reload\`` | `web/src/api/index.ts:1172` | [FAIL] |
| POST | `/api/v1/ai/models/{var}/rollback` | `\`/ai/models/${id}/rollback\`` | `web/src/api/index.ts:1206` | [FAIL] |
| POST | `/api/v1/ai/models/{var}/schedule` | `\`/ai/models/${id}/schedule\`` | `web/src/api/index.ts:1189` | [FAIL] |
| DELETE | `/api/v1/ai/models/{var}/schedule` | `\`/ai/models/${id}/schedule\`` | `web/src/api/index.ts:1191` | [FAIL] |
| GET | `/api/v1/ai/models/{var}/stats` | `\`/ai/models/${id}/stats\`` | `web/src/api/index.ts:1180` | [FAIL] |
| GET | `/api/v1/ai/models/{var}/versions` | `\`/ai/models/${id}/versions\`` | `web/src/api/index.ts:1204` | [FAIL] |
| GET | `/api/v1/ai/schedules` | `'/ai/schedules'` | `web/src/api/index.ts:1193` | [OK] |
| GET | `/api/v1/ai/stats` | `'/ai/stats'` | `web/src/api/index.ts:1176` | [OK] |
| GET | `/api/v1/ai/summary` | `'/ai/summary'` | `web/src/api/index.ts:1178` | [OK] |

### 3.4 API `aiEnhancedApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/ai/ab-test` | `'/ai/ab-test'` | `web/src/api/index.ts:1213` | [OK] |
| GET | `/api/v1/ai/ab-test/{var}` | `\`/ai/ab-test/${modelId}\`` | `web/src/api/index.ts:1215` | [FAIL] |
| DELETE | `/api/v1/ai/ab-test/{var}` | `\`/ai/ab-test/${modelId}\`` | `web/src/api/index.ts:1219` | [FAIL] |
| POST | `/api/v1/ai/ab-test/{var}/promote` | `\`/ai/ab-test/${modelId}/promote\`` | `web/src/api/index.ts:1221` | [FAIL] |
| POST | `/api/v1/ai/ab-test/{var}/rollback` | `\`/ai/ab-test/${modelId}/rollback\`` | `web/src/api/index.ts:1223` | [FAIL] |
| PUT | `/api/v1/ai/ab-test/{var}/split` | `\`/ai/ab-test/${modelId}/split\`` | `web/src/api/index.ts:1217` | [FAIL] |
| GET | `/api/v1/ai/batch/stats` | `'/ai/batch/stats'` | `web/src/api/index.ts:1247` | [OK] |
| POST | `/api/v1/ai/cache/clear` | `'/ai/cache/clear'` | `web/src/api/index.ts:1239` | [OK] |
| GET | `/api/v1/ai/cache/stats` | `'/ai/cache/stats'` | `web/src/api/index.ts:1237` | [OK] |
| GET | `/api/v1/ai/devices` | `'/ai/devices'` | `web/src/api/index.ts:1245` | [OK] |
| POST | `/api/v1/ai/hot-swap` | `'/ai/hot-swap'` | `web/src/api/index.ts:1225` | [OK] |
| GET | `/api/v1/ai/hot-swap/{var}` | `\`/ai/hot-swap/${modelId}\`` | `web/src/api/index.ts:1227` | [FAIL] |
| GET | `/api/v1/ai/latency/{var}` | `\`/ai/latency/${modelId}\`` | `web/src/api/index.ts:1243` | [FAIL] |
| PUT | `/api/v1/ai/models/{var}/postprocess` | `\`/ai/models/${modelId}/postprocess\`` | `web/src/api/index.ts:1231` | [FAIL] |
| PUT | `/api/v1/ai/models/{var}/preprocess` | `\`/ai/models/${modelId}/preprocess\`` | `web/src/api/index.ts:1229` | [FAIL] |
| GET | `/api/v1/ai/postprocess/steps` | `'/ai/postprocess/steps'` | `web/src/api/index.ts:1235` | [OK] |
| GET | `/api/v1/ai/preprocess/steps` | `'/ai/preprocess/steps'` | `web/src/api/index.ts:1233` | [OK] |
| GET | `/api/v1/ai/resources` | `'/ai/resources'` | `web/src/api/index.ts:1241` | [OK] |

### 3.5 API `alarmApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/alarms` | `'/alarms'` | `web/src/api/index.ts:324` | [OK] |
| POST | `/api/v1/alarms/batch-ack` | `'/alarms/batch-ack'` | `web/src/api/index.ts:334` | [OK] |
| GET | `/api/v1/alarms/correlation` | `'/alarms/correlation'` | `web/src/api/index.ts:355` | [OK] |
| GET | `/api/v1/alarms/history/{var}` | `\`/alarms/history/${encodeURIComponent(ruleId)}\`` | `web/src/api/index.ts:361` | [FAIL] |
| GET | `/api/v1/alarms/statistics` | `'/alarms/statistics'` | `web/src/api/index.ts:340` | [OK] |
| GET | `/api/v1/alarms/trend` | `'/alarms/trend'` | `web/src/api/index.ts:344` | [OK] |
| GET | `/api/v1/alarms/{var}` | `\`/alarms/${id}\`` | `web/src/api/index.ts:327` | [FAIL] |
| PUT | `/api/v1/alarms/{var}/ack` | `\`/alarms/${id}/ack\`` | `web/src/api/index.ts:330` | [FAIL] |
| PUT | `/api/v1/alarms/{var}/recover` | `\`/alarms/${id}/recover\`` | `web/src/api/index.ts:337` | [FAIL] |
| POST | `/api/v1/alarms/{var}/suppress` | `\`/alarms/${alarmId}/suppress\`` | `web/src/api/index.ts:348` | [FAIL] |

### 3.6 API `alarmSilenceApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/alarms/silence` | `'/alarms/silence'` | `web/src/api/index.ts:1742` | [OK] |
| POST | `/api/v1/alarms/silence` | `'/alarms/silence'` | `web/src/api/index.ts:1745` | [OK] |
| DELETE | `/api/v1/alarms/silence/{var}` | `\`/alarms/silence/${silenceId}\`` | `web/src/api/index.ts:1748` | [FAIL] |
| DELETE | `/api/v1/alarms/silence/{var}` | `\`/alarms/silence/${silenceId}\`` | `web/src/api/index.ts:1751` | [FAIL] |

### 3.7 API `anomalyLearnerApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/anomaly-learner/dashboard` | `'/anomaly-learner/dashboard'` | `web/src/api/index.ts:1260` | [OK] |
| POST | `/api/v1/anomaly-learner/feedback` | `'/anomaly-learner/feedback'` | `web/src/api/index.ts:1258` | [OK] |
| POST | `/api/v1/anomaly-learner/infer` | `'/anomaly-learner/infer'` | `web/src/api/index.ts:1256` | [OK] |
| POST | `/api/v1/anomaly-learner/initialize` | `'/anomaly-learner/initialize'` | `web/src/api/index.ts:1254` | [OK] |
| GET | `/api/v1/anomaly-learner/status` | `'/anomaly-learner/status'` | `web/src/api/index.ts:1262` | [OK] |

### 3.8 API `appUpdateApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/ota/apply` | `URL.OTA.APPLY` | `web/src/api/index.ts:1090` | [OK] |
| GET | `/api/v1/ota/backups` | `URL.OTA.BACKUPS` | `web/src/api/index.ts:1096` | [OK] |
| POST | `/api/v1/ota/cancel` | `URL.OTA.CANCEL` | `web/src/api/index.ts:1102` | [OK] |
| GET | `/api/v1/ota/check` | `URL.OTA.CHECK` | `web/src/api/index.ts:1087` | [OK] |
| POST | `/api/v1/ota/rollback` | `URL.OTA.ROLLBACK` | `web/src/api/index.ts:1093` | [OK] |
| GET | `/api/v1/ota/status` | `URL.OTA.STATUS` | `web/src/api/index.ts:1099` | [OK] |

### 3.9 API `auditApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/audit/cleanup` | `'/audit/cleanup'` | `web/src/api/index.ts:812` | [OK] |
| GET | `/api/v1/audit/export/csv` | `'/audit/export/csv'` | `web/src/api/index.ts:809` | [OK] |
| GET | `/api/v1/audit/integrity` | `'/audit/integrity'` | `web/src/api/index.ts:806` | [OK] |
| GET | `/api/v1/audit/logs` | `'/audit/logs'` | `web/src/api/index.ts:803` | [OK] |

### 3.10 API `authApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/auth/change-password` | `'/auth/change-password'` | `web/src/api/index.ts:58` | [OK] |
| POST | `/api/v1/auth/login` | `'/auth/login'` | `web/src/api/index.ts:44` | [OK] |
| POST | `/api/v1/auth/logout` | `'/auth/logout'` | `web/src/api/index.ts:54` | [OK] |
| GET | `/api/v1/auth/me` | `'/auth/me'` | `web/src/api/index.ts:50` | [OK] |
| POST | `/api/v1/auth/refresh` | `'/auth/refresh'` | `web/src/api/index.ts:47` | [OK] |

### 3.11 API `bridgeApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/bridge/create` | `'/bridge/create'` | `web/src/api/index.ts:1554` | [OK] |
| GET | `/api/v1/bridge/list` | `'/bridge/list'` | `web/src/api/index.ts:1548` | [OK] |
| GET | `/api/v1/bridge/{var}` | `\`/bridge/${encodeURIComponent(name)}\`` | `web/src/api/index.ts:1551` | [FAIL] |
| PUT | `/api/v1/bridge/{var}` | `\`/bridge/${encodeURIComponent(name)}\`` | `web/src/api/index.ts:1557` | [FAIL] |
| DELETE | `/api/v1/bridge/{var}` | `\`/bridge/${encodeURIComponent(name)}\`` | `web/src/api/index.ts:1560` | [FAIL] |
| POST | `/api/v1/bridge/{var}/disable` | `\`/bridge/${encodeURIComponent(name)}/disable\`` | `web/src/api/index.ts:1566` | [FAIL] |
| POST | `/api/v1/bridge/{var}/enable` | `\`/bridge/${encodeURIComponent(name)}/enable\`` | `web/src/api/index.ts:1563` | [FAIL] |

### 3.12 API `configVersionApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/config/rollback/{var}` | `\`/config/rollback/${versionId}\`` | `web/src/api/index.ts:1710` | [FAIL] |
| GET | `/api/v1/config/versions` | `'/config/versions'` | `web/src/api/index.ts:1701` | [OK] |
| GET | `/api/v1/config/versions/diff` | `'/config/versions/diff'` | `web/src/api/index.ts:1707` | [OK] |
| POST | `/api/v1/config/versions/snapshot` | `'/config/versions/snapshot'` | `web/src/api/index.ts:1713` | [OK] |
| GET | `/api/v1/config/versions/{var}` | `\`/config/versions/${versionId}\`` | `web/src/api/index.ts:1704` | [FAIL] |
| DELETE | `/api/v1/config/versions/{var}` | `\`/config/versions/${versionId}\`` | `web/src/api/index.ts:1716` | [FAIL] |

### 3.13 API `dataApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/data-quality/trend` | `'/data-quality/trend'` | `web/src/api/index.ts:392` | [OK] |
| GET | `/api/v1/data/correlation` | `'/data/correlation'` | `web/src/api/index.ts:383` | [OK] |
| POST | `/api/v1/data/downsample` | `'/data/downsample'` | `web/src/api/index.ts:396` | [OK] |
| GET | `/api/v1/data/export` | `'/data/export'` | `web/src/api/index.ts:373` | [OK] |
| GET | `/api/v1/data/multi-point` | `'/data/multi-point'` | `web/src/api/index.ts:389` | [OK] |
| GET | `/api/v1/data/query` | `'/data/query'` | `web/src/api/index.ts:370` | [OK] |
| GET | `/api/v1/data/statistics` | `'/data/statistics'` | `web/src/api/index.ts:386` | [OK] |
| GET | `/api/v1/data/stats` | `'/data/stats'` | `web/src/api/index.ts:376` | [OK] |
| GET | `/api/v1/data/trend` | `'/data/trend'` | `web/src/api/index.ts:380` | [OK] |

### 3.14 API `dataMigrationApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/data/export` | `'/data/export'` | `web/src/api/index.ts:2184` | [OK] |
| POST | `/api/v1/data/import` | `'/data/import'` | `web/src/api/index.ts:2188` | [OK] |

### 3.15 API `dbMonitorApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/db-monitor/pool-stats` | `'/db-monitor/pool-stats'` | `web/src/api/index.ts:583` | [OK] |
| GET | `/api/v1/db-monitor/slow-queries` | `'/db-monitor/slow-queries'` | `web/src/api/index.ts:594` | [OK] |

### 3.16 API `debugApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/debug/devices` | `'/debug/devices'` | `web/src/api/index.ts:1515` | [OK] |
| GET | `/api/v1/debug/packets` | `'/debug/packets'` | `web/src/api/index.ts:1518` | [OK] |
| DELETE | `/api/v1/debug/packets` | `'/debug/packets'` | `web/src/api/index.ts:1521` | [OK] |
| GET | `/api/v1/debug/protocols` | `'/debug/protocols'` | `web/src/api/index.ts:1512` | [OK] |
| POST | `/api/v1/debug/simulate` | `'/debug/simulate'` | `web/src/api/index.ts:1524` | [OK] |

### 3.17 API `deviceApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/devices` | `'/devices'` | `web/src/api/index.ts:115` | [OK] |
| POST | `/api/v1/devices` | `'/devices'` | `web/src/api/index.ts:121` | [OK] |
| POST | `/api/v1/devices/batch-deploy` | `'/devices/batch-deploy'` | `web/src/api/index.ts:205` | [OK] |
| POST | `/api/v1/devices/batch/start-collect` | `'/devices/batch/start-collect'` | `web/src/api/index.ts:212` | [OK] |
| POST | `/api/v1/devices/batch/stop-collect` | `'/devices/batch/stop-collect'` | `web/src/api/index.ts:215` | [OK] |
| GET | `/api/v1/devices/collect-stats` | `'/devices/collect-stats'` | `web/src/api/index.ts:161` | [OK] |
| GET | `/api/v1/devices/device-quality-stats` | `'/devices/device-quality-stats'` | `web/src/api/index.ts:164` | [OK] |
| POST | `/api/v1/devices/discover` | `'/devices/discover'` | `web/src/api/index.ts:146` | [OK] |
| GET | `/api/v1/devices/health` | `'/devices/health'` | `web/src/api/index.ts:202` | [OK] |
| GET | `/api/v1/devices/health/all` | `'/devices/health/all'` | `web/src/api/index.ts:187` | [OK] |
| POST | `/api/v1/devices/import` | `'/devices/import'` | `web/src/api/index.ts:224` | [OK] |
| POST | `/api/v1/devices/simulator` | `'/devices/simulator'` | `web/src/api/index.ts:143` | [OK] |
| POST | `/api/v1/devices/test-connection` | `'/devices/test-connection'` | `web/src/api/index.ts:150` | [OK] |
| GET | `/api/v1/devices/{var}` | `\`/devices/${id}\`` | `web/src/api/index.ts:118` | [FAIL] |
| PUT | `/api/v1/devices/{var}` | `\`/devices/${id}\`` | `web/src/api/index.ts:124` | [FAIL] |
| DELETE | `/api/v1/devices/{var}` | `\`/devices/${id}\`` | `web/src/api/index.ts:133` | [FAIL] |
| GET | `/api/v1/devices/{var}/health` | `\`/devices/${id}/health\`` | `web/src/api/index.ts:167` | [FAIL] |
| GET | `/api/v1/devices/{var}/health` | `\`/devices/${id}/health\`` | `web/src/api/index.ts:173` | [FAIL] |
| POST | `/api/v1/devices/{var}/health/reset` | `\`/devices/${id}/health/reset\`` | `web/src/api/index.ts:170` | [FAIL] |
| GET | `/api/v1/devices/{var}/ops` | `\`/devices/${id}/ops\`` | `web/src/api/index.ts:176` | [FAIL] |
| GET | `/api/v1/devices/{var}/point-health` | `\`/devices/${id}/point-health\`` | `web/src/api/index.ts:179` | [FAIL] |
| GET | `/api/v1/devices/{var}/points` | `\`/devices/${id}/points\`` | `web/src/api/index.ts:136` | [FAIL] |
| POST | `/api/v1/devices/{var}/points` | `\`/devices/${id}/points\`` | `web/src/api/index.ts:140` | [FAIL] |
| POST | `/api/v1/devices/{var}/push` | `\`/devices/${deviceId}/push\`` | `web/src/api/index.ts:156` | [FAIL] |
| GET | `/api/v1/devices/{var}/write-audit` | `\`/devices/${id}/write-audit\`` | `web/src/api/index.ts:182` | [FAIL] |
| PUT | `/api/v1/devices/{var}/write-policy` | `\`/devices/${id}/write-policy\`` | `web/src/api/index.ts:128` | [FAIL] |

### 3.18 API `driverApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/drivers` | `'/drivers'` | `web/src/api/index.ts:636` | [OK] |
| GET | `/api/v1/drivers/list` | `'/drivers/list'` | `web/src/api/index.ts:630` | [OK] |
| GET | `/api/v1/drivers/load-status` | `'/drivers/load-status'` | `web/src/api/index.ts:645` | [OK] |
| GET | `/api/v1/drivers/meta` | `'/drivers/meta'` | `web/src/api/index.ts:648` | [OK] |
| GET | `/api/v1/drivers/opc-da/servers` | `'/drivers/opc-da/servers'` | `web/src/api/index.ts:654` | [OK] |
| POST | `/api/v1/drivers/opcua/browse` | `'/drivers/opcua/browse'` | `web/src/api/index.ts:651` | [OK] |
| GET | `/api/v1/drivers/protocols` | `'/drivers/protocols'` | `web/src/api/index.ts:633` | [OK] |
| GET | `/api/v1/drivers/{var}/config-schema` | `\`/drivers/${driverName}/config-schema\`` | `web/src/api/index.ts:639` | [FAIL] |
| POST | `/api/v1/drivers/{var}/discover` | `\`/drivers/${driverName}/discover\`` | `web/src/api/index.ts:642` | [FAIL] |

### 3.19 API `expressionApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/expressions/evaluate` | `'/expressions/evaluate'` | `web/src/api/index.ts:819` | [OK] |
| POST | `/api/v1/expressions/evaluate-batch` | `'/expressions/evaluate-batch'` | `web/src/api/index.ts:822` | [OK] |
| GET | `/api/v1/expressions/functions` | `'/expressions/functions'` | `web/src/api/index.ts:828` | [OK] |
| POST | `/api/v1/expressions/validate` | `'/expressions/validate'` | `web/src/api/index.ts:825` | [OK] |

### 3.20 API `finsOpsApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/devices/{var}/ops` | `\`/devices/${deviceId}/ops\`` | `web/src/api/index.ts:1965` | [FAIL] |
| GET | `/api/v1/devices/{var}/point-health` | `\`/devices/${deviceId}/point-health\`` | `web/src/api/index.ts:1968` | [FAIL] |
| GET | `/api/v1/devices/{var}/write-audit` | `\`/devices/${deviceId}/write-audit\`` | `web/src/api/index.ts:1971` | [FAIL] |

### 3.21 API `firmwareApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/firmware/manifest/generate` | `'/firmware/manifest/generate'` | `web/src/api/index.ts:1683` | [OK] |
| POST | `/api/v1/firmware/verify/hash` | `'/firmware/verify/hash'` | `web/src/api/index.ts:1674` | [OK] |
| POST | `/api/v1/firmware/verify/signature` | `'/firmware/verify/signature'` | `web/src/api/index.ts:1666` | [OK] |

### 3.22 API `grafanaApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/grafana/config` | `'/grafana/config'` | `web/src/api/index.ts:1109` | [OK] |
| GET | `/api/v1/grafana/dashboards` | `'/grafana/dashboards'` | `web/src/api/index.ts:1112` | [OK] |
| GET | `/api/v1/grafana/embed-url` | `'/grafana/embed-url'` | `web/src/api/index.ts:1115` | [OK] |

### 3.23 API `integrationApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/integration/handshake` | `'/integration/handshake'` | `web/src/api/index.ts:1130` | [OK] |
| POST | `/api/v1/integration/rpc/execute` | `'/integration/rpc/execute'` | `web/src/api/index.ts:1136` | [OK] |
| GET | `/api/v1/integration/rpc/history` | `'/integration/rpc/history'` | `web/src/api/index.ts:1139` | [OK] |
| GET | `/api/v1/integration/status` | `'/integration/status'` | `web/src/api/index.ts:1133` | [OK] |

### 3.24 API `linkageApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/linkage/executions` | `'/linkage/executions'` | `web/src/api/index.ts:1595` | [OK] |
| GET | `/api/v1/linkage/executions/stats` | `'/linkage/executions/stats'` | `web/src/api/index.ts:1598` | [OK] |
| GET | `/api/v1/linkage/rules` | `'/linkage/rules'` | `web/src/api/index.ts:1573` | [OK] |
| POST | `/api/v1/linkage/rules` | `'/linkage/rules'` | `web/src/api/index.ts:1579` | [OK] |
| GET | `/api/v1/linkage/rules/{var}` | `\`/linkage/rules/${ruleId}\`` | `web/src/api/index.ts:1576` | [FAIL] |
| PUT | `/api/v1/linkage/rules/{var}` | `\`/linkage/rules/${ruleId}\`` | `web/src/api/index.ts:1582` | [FAIL] |
| DELETE | `/api/v1/linkage/rules/{var}` | `\`/linkage/rules/${ruleId}\`` | `web/src/api/index.ts:1585` | [FAIL] |
| POST | `/api/v1/linkage/rules/{var}/disable` | `\`/linkage/rules/${ruleId}/disable\`` | `web/src/api/index.ts:1591` | [FAIL] |
| POST | `/api/v1/linkage/rules/{var}/enable` | `\`/linkage/rules/${ruleId}/enable\`` | `web/src/api/index.ts:1588` | [FAIL] |

### 3.25 API `logApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/logs/archive` | `'/logs/archive'` | `web/src/api/index.ts:1651` | [OK] |
| POST | `/api/v1/logs/cleanup` | `'/logs/cleanup'` | `web/src/api/index.ts:1654` | [OK] |
| POST | `/api/v1/logs/filters` | `'/logs/filters'` | `web/src/api/index.ts:1642` | [FAIL] |
| DELETE | `/api/v1/logs/filters` | `'/logs/filters'` | `web/src/api/index.ts:1645` | [FAIL] |
| POST | `/api/v1/logs/level` | `'/logs/level'` | `web/src/api/index.ts:1648` | [FAIL] |
| GET | `/api/v1/logs/query` | `'/logs/query'` | `web/src/api/index.ts:1636` | [OK] |
| GET | `/api/v1/logs/stats` | `'/logs/stats'` | `web/src/api/index.ts:1639` | [OK] |

### 3.26 API `mcpApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/mcp/auth-keys` | `'/mcp/auth-keys'` | `web/src/api/index.ts:970` | [OK] |
| POST | `/api/v1/mcp/auth-keys` | `'/mcp/auth-keys'` | `web/src/api/index.ts:974` | [OK] |
| DELETE | `/api/v1/mcp/auth-keys/{var}` | `\`/mcp/auth-keys/${keyId}\`` | `web/src/api/index.ts:978` | [FAIL] |
| POST | `/api/v1/mcp/call` | `'/mcp/call'` | `web/src/api/index.ts:961` | [OK] |
| GET | `/api/v1/mcp/prompts` | `'/mcp/prompts'` | `web/src/api/index.ts:967` | [OK] |
| GET | `/api/v1/mcp/resources` | `'/mcp/resources'` | `web/src/api/index.ts:964` | [OK] |
| GET | `/api/v1/mcp/tools` | `'/mcp/tools'` | `web/src/api/index.ts:958` | [OK] |

### 3.27 API `modbusOpsApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/devices/{var}/ops` | `\`/devices/${deviceId}/ops\`` | `web/src/api/index.ts:1905` | [FAIL] |
| GET | `/api/v1/devices/{var}/point-health` | `\`/devices/${deviceId}/point-health\`` | `web/src/api/index.ts:1908` | [FAIL] |
| GET | `/api/v1/devices/{var}/write-audit` | `\`/devices/${deviceId}/write-audit\`` | `web/src/api/index.ts:1911` | [FAIL] |

### 3.28 API `modbusSlaveApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| PUT | `/api/v1/modbus-slave/config` | `'/modbus-slave/config'` | `web/src/api/index.ts:946` | [OK] |
| POST | `/api/v1/modbus-slave/start` | `'/modbus-slave/start'` | `web/src/api/index.ts:938` | [OK] |
| GET | `/api/v1/modbus-slave/status` | `'/modbus-slave/status'` | `web/src/api/index.ts:934` | [OK] |
| POST | `/api/v1/modbus-slave/stop` | `'/modbus-slave/stop'` | `web/src/api/index.ts:942` | [OK] |

### 3.29 API `modbusSlaveOpsApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/modbus-slave/devices/{var}/ops` | `\`/modbus-slave/devices/${deviceId}/ops\`` | `web/src/api/index.ts:1947` | [FAIL] |

### 3.30 API `mqttForwarderApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/mqtt-forwarder/offline-queue/status` | `'/mqtt-forwarder/offline-queue/status'` | `web/src/api/index.ts:1420` | [OK] |

### 3.31 API `mqttServerApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| PUT | `/api/v1/mqtt-server/config` | `'/mqtt-server/config'` | `web/src/api/index.ts:918` | [OK] |
| POST | `/api/v1/mqtt-server/start` | `'/mqtt-server/start'` | `web/src/api/index.ts:910` | [OK] |
| GET | `/api/v1/mqtt-server/status` | `'/mqtt-server/status'` | `web/src/api/index.ts:906` | [OK] |
| POST | `/api/v1/mqtt-server/stop` | `'/mqtt-server/stop'` | `web/src/api/index.ts:914` | [OK] |

### 3.32 API `notifyApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/notify/channels` | `'/notify/channels'` | `web/src/api/index.ts:1351` | [OK] |
| POST | `/api/v1/notify/channels/dingtalk` | `'/notify/channels/dingtalk'` | `web/src/api/index.ts:1354` | [OK] |
| POST | `/api/v1/notify/channels/email` | `'/notify/channels/email'` | `web/src/api/index.ts:1360` | [OK] |
| POST | `/api/v1/notify/channels/webhook` | `'/notify/channels/webhook'` | `web/src/api/index.ts:1363` | [OK] |
| POST | `/api/v1/notify/channels/wecom` | `'/notify/channels/wecom'` | `web/src/api/index.ts:1357` | [OK] |
| DELETE | `/api/v1/notify/channels/{var}` | `\`/notify/channels/${channelId}\`` | `web/src/api/index.ts:1372` | [FAIL] |
| POST | `/api/v1/notify/channels/{var}/enable` | `\`/notify/channels/${channelId}/enable\`` | `web/src/api/index.ts:1369` | [FAIL] |
| POST | `/api/v1/notify/channels/{var}/test` | `\`/notify/channels/${channelId}/test\`` | `web/src/api/index.ts:1366` | [FAIL] |

### 3.33 API `observabilityApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/observability/alerts/events` | `'/observability/alerts/events'` | `web/src/api/index.ts:2125` | [OK] |
| POST | `/api/v1/observability/alerts/events/{var}/{var}/resolve` | `\`/observability/alerts/events/${encodeURIComponent(ruleName)}/${timestamp}/resolve\`` | `web/src/api/index.ts:2128` | [FAIL] |
| GET | `/api/v1/observability/alerts/rules` | `'/observability/alerts/rules'` | `web/src/api/index.ts:2111` | [OK] |
| POST | `/api/v1/observability/alerts/rules` | `'/observability/alerts/rules'` | `web/src/api/index.ts:2114` | [FAIL] |
| PUT | `/api/v1/observability/alerts/rules/{var}` | `\`/observability/alerts/rules/${encodeURIComponent(ruleName)}\`` | `web/src/api/index.ts:2117` | [FAIL] |
| DELETE | `/api/v1/observability/alerts/rules/{var}` | `\`/observability/alerts/rules/${encodeURIComponent(ruleName)}\`` | `web/src/api/index.ts:2120` | [FAIL] |
| GET | `/api/v1/observability/latency` | `'/observability/latency'` | `web/src/api/index.ts:2101` | [OK] |
| GET | `/api/v1/observability/latency/histogram` | `'/observability/latency/histogram'` | `web/src/api/index.ts:2107` | [OK] |
| GET | `/api/v1/observability/latency/percentiles` | `'/observability/latency/percentiles'` | `web/src/api/index.ts:2104` | [OK] |
| GET | `/api/v1/observability/metrics` | `'/observability/metrics'` | `web/src/api/index.ts:2144` | [OK] |
| GET | `/api/v1/observability/overview` | `'/observability/overview'` | `web/src/api/index.ts:2097` | [OK] |
| GET | `/api/v1/observability/traces` | `'/observability/traces'` | `web/src/api/index.ts:2132` | [OK] |
| GET | `/api/v1/observability/traces/stats/{var}` | `\`/observability/traces/stats/${encodeURIComponent(nodeName)}\`` | `web/src/api/index.ts:2138` | [FAIL] |
| GET | `/api/v1/observability/traces/{var}` | `\`/observability/traces/${encodeURIComponent(messageId)}\`` | `web/src/api/index.ts:2135` | [FAIL] |

### 3.34 API `platformApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/platforms/alarm-records/{var}` | `\`/platforms/alarm-records/${platformName}\`` | `web/src/api/index.ts:757` | [FAIL] |
| GET | `/api/v1/platforms/broker-quality/{var}` | `\`/platforms/broker-quality/${platformName}\`` | `web/src/api/index.ts:733` | [FAIL] |
| GET | `/api/v1/platforms/broker-status/{var}` | `\`/platforms/broker-status/${platformName}\`` | `web/src/api/index.ts:769` | [FAIL] |
| GET | `/api/v1/platforms/command-logs/{var}` | `\`/platforms/command-logs/${platformName}\`` | `web/src/api/index.ts:754` | [FAIL] |
| GET | `/api/v1/platforms/config-schema/{var}` | `\`/platforms/config-schema/${platformName}\`` | `web/src/api/index.ts:706` | [FAIL] |
| POST | `/api/v1/platforms/connect/{var}` | `\`/platforms/connect/${platformName}\`` | `web/src/api/index.ts:709` | [FAIL] |
| GET | `/api/v1/platforms/dashboard` | `'/platforms/dashboard'` | `web/src/api/index.ts:721` | [OK] |
| GET | `/api/v1/platforms/device-mapping/{var}` | `\`/platforms/device-mapping/${platformName}\`` | `web/src/api/index.ts:760` | [FAIL] |
| POST | `/api/v1/platforms/disconnect/{var}` | `\`/platforms/disconnect/${platformName}\`` | `web/src/api/index.ts:712` | [FAIL] |
| GET | `/api/v1/platforms/export/{var}` | `\`/platforms/export/${platformName}\`` | `web/src/api/index.ts:763` | [FAIL] |
| POST | `/api/v1/platforms/import/{var}` | `\`/platforms/import/${platformName}\`` | `web/src/api/index.ts:766` | [FAIL] |
| GET | `/api/v1/platforms/list` | `'/platforms/list'` | `web/src/api/index.ts:703` | [OK] |
| GET | `/api/v1/platforms/message-preview/{var}` | `\`/platforms/message-preview/${platformName}\`` | `web/src/api/index.ts:730` | [FAIL] |
| GET | `/api/v1/platforms/metrics` | `'/platforms/metrics'` | `web/src/api/index.ts:724` | [OK] |
| POST | `/api/v1/platforms/mqtt-test-publish/{var}` | `\`/platforms/mqtt-test-publish/${platformName}\`` | `web/src/api/index.ts:784` | [FAIL] |
| POST | `/api/v1/platforms/preview-template` | `'/platforms/preview-template'` | `web/src/api/index.ts:775` | [OK] |
| POST | `/api/v1/platforms/reload/{var}` | `\`/platforms/reload/${platformName}\`` | `web/src/api/index.ts:727` | [FAIL] |
| GET | `/api/v1/platforms/shadow/{var}` | `\`/platforms/shadow/${platformName}\`` | `web/src/api/index.ts:751` | [FAIL] |
| GET | `/api/v1/platforms/status/{var}` | `\`/platforms/status/${platformName}\`` | `web/src/api/index.ts:715` | [FAIL] |
| GET | `/api/v1/platforms/tb/alarms/{var}` | `\`/platforms/tb/alarms/${platformName}\`` | `web/src/api/index.ts:745` | [FAIL] |
| GET | `/api/v1/platforms/tb/devices/{var}` | `\`/platforms/tb/devices/${platformName}\`` | `web/src/api/index.ts:739` | [FAIL] |
| GET | `/api/v1/platforms/tb/rpc-logs/{var}` | `\`/platforms/tb/rpc-logs/${platformName}\`` | `web/src/api/index.ts:742` | [FAIL] |
| GET | `/api/v1/platforms/tb/sync-status/{var}` | `\`/platforms/tb/sync-status/${platformName}\`` | `web/src/api/index.ts:748` | [FAIL] |
| POST | `/api/v1/platforms/test-connection/{var}` | `\`/platforms/test-connection/${platformName}\`` | `web/src/api/index.ts:718` | [FAIL] |
| POST | `/api/v1/platforms/test-script` | `'/platforms/test-script'` | `web/src/api/index.ts:781` | [OK] |
| POST | `/api/v1/platforms/validate-advanced-template` | `'/platforms/validate-advanced-template'` | `web/src/api/index.ts:772` | [OK] |
| POST | `/api/v1/platforms/validate-script` | `'/platforms/validate-script'` | `web/src/api/index.ts:778` | [OK] |
| POST | `/api/v1/platforms/validate-topic` | `'/platforms/validate-topic'` | `web/src/api/index.ts:736` | [OK] |

### 3.35 API `preprocessApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/preprocess/config` | `'/preprocess/config'` | `web/src/api/index.ts:673` | [OK] |
| PUT | `/api/v1/preprocess/config` | `'/preprocess/config'` | `web/src/api/index.ts:677` | [OK] |

### 3.36 API `profilerApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/profiler/disable` | `'/profiler/disable'` | `web/src/api/index.ts:1620` | [OK] |
| POST | `/api/v1/profiler/enable` | `'/profiler/enable'` | `web/src/api/index.ts:1617` | [OK] |
| GET | `/api/v1/profiler/export` | `'/profiler/export'` | `web/src/api/index.ts:1626` | [OK] |
| GET | `/api/v1/profiler/memory` | `'/profiler/memory'` | `web/src/api/index.ts:1611` | [OK] |
| GET | `/api/v1/profiler/requests` | `'/profiler/requests'` | `web/src/api/index.ts:1614` | [OK] |
| POST | `/api/v1/profiler/reset` | `'/profiler/reset'` | `web/src/api/index.ts:1623` | [OK] |
| GET | `/api/v1/profiler/slowest` | `'/profiler/slowest'` | `web/src/api/index.ts:1608` | [OK] |
| GET | `/api/v1/profiler/stats` | `'/profiler/stats'` | `web/src/api/index.ts:1605` | [OK] |

### 3.37 API `qualityMonitorApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/data-quality/devices` | `'/data-quality/devices'` | `web/src/api/index.ts:1760` | [OK] |
| GET | `/api/v1/data-quality/devices/{var}` | `\`/data-quality/devices/${deviceId}\`` | `web/src/api/index.ts:1762` | [FAIL] |
| GET | `/api/v1/data-quality/devices/{var}/points` | `\`/data-quality/devices/${deviceId}/points\`` | `web/src/api/index.ts:1764` | [FAIL] |
| GET | `/api/v1/data-quality/report` | `'/data-quality/report'` | `web/src/api/index.ts:1768` | [OK] |
| POST | `/api/v1/data-quality/reset` | `'/data-quality/reset'` | `web/src/api/index.ts:1770` | [OK] |
| GET | `/api/v1/data-quality/summary` | `'/data-quality/summary'` | `web/src/api/index.ts:1758` | [OK] |
| GET | `/api/v1/data-quality/trend` | `'/data-quality/trend'` | `web/src/api/index.ts:1766` | [OK] |

### 3.38 API `resourceShareApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/resource-shares` | `URL.RESOURCE_SHARES.BASE` | `web/src/api/index.ts:1809` | [OK] |
| DELETE | `/api/v1/resource-shares` | `URL.RESOURCE_SHARES.BASE` | `web/src/api/index.ts:1817` | [OK] |
| GET | `/api/v1/resource-shares/resource/{var}/{var}` | `URL.RESOURCE_SHARES.RESOURCE(resourceType, resourceId)` | `web/src/api/index.ts:1821` | [FAIL] |
| POST | `/api/v1/resource-shares/transfer` | `URL.RESOURCE_SHARES.TRANSFER` | `web/src/api/index.ts:1830` | [FAIL] |

### 3.39 API `ruleApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/rules` | `'/rules'` | `web/src/api/index.ts:271` | [OK] |
| POST | `/api/v1/rules` | `'/rules'` | `web/src/api/index.ts:277` | [OK] |
| GET | `/api/v1/rules/{var}` | `\`/rules/${id}\`` | `web/src/api/index.ts:274` | [FAIL] |
| PUT | `/api/v1/rules/{var}` | `\`/rules/${id}\`` | `web/src/api/index.ts:280` | [FAIL] |
| DELETE | `/api/v1/rules/{var}` | `\`/rules/${id}\`` | `web/src/api/index.ts:284` | [FAIL] |
| POST | `/api/v1/rules/{var}/disable` | `\`/rules/${id}/disable\`` | `web/src/api/index.ts:290` | [FAIL] |
| POST | `/api/v1/rules/{var}/enable` | `\`/rules/${id}/enable\`` | `web/src/api/index.ts:287` | [FAIL] |
| POST | `/api/v1/rules/{var}/test` | `\`/rules/${id}/test\`` | `web/src/api/index.ts:293` | [FAIL] |
| GET | `/api/v1/rules/{var}/versions` | `\`/rules/${id}/versions\`` | `web/src/api/index.ts:296` | [FAIL] |
| POST | `/api/v1/rules/{var}/versions/rollback` | `\`/rules/${id}/versions/rollback\`` | `web/src/api/index.ts:300` | [FAIL] |
| GET | `/api/v1/rules/{var}/versions/{var}` | `\`/rules/${id}/versions/${version}\`` | `web/src/api/index.ts:298` | [FAIL] |

### 3.40 API `scadaApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/scada/project` | `'/scada/project'` | `web/src/api/index.ts:1153` | [OK] |
| GET | `/api/v1/scada/project/{var}` | `\`/scada/project/${encodeURIComponent(name)}\`` | `web/src/api/index.ts:1149` | [FAIL] |
| DELETE | `/api/v1/scada/project/{var}` | `\`/scada/project/${encodeURIComponent(name)}\`` | `web/src/api/index.ts:1157` | [FAIL] |
| GET | `/api/v1/scada/projects` | `'/scada/projects'` | `web/src/api/index.ts:1146` | [OK] |

### 3.41 API `scriptApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/scripts/create` | `'/scripts/create'` | `web/src/api/index.ts:1839` | [OK] |
| GET | `/api/v1/scripts/list` | `'/scripts/list'` | `web/src/api/index.ts:1837` | [OK] |
| PUT | `/api/v1/scripts/{var}` | `\`/scripts/${scriptId}\`` | `web/src/api/index.ts:1841` | [FAIL] |
| DELETE | `/api/v1/scripts/{var}` | `\`/scripts/${scriptId}\`` | `web/src/api/index.ts:1843` | [FAIL] |
| POST | `/api/v1/scripts/{var}/approve` | `\`/scripts/${scriptId}/approve\`` | `web/src/api/index.ts:1856` | [FAIL] |
| POST | `/api/v1/scripts/{var}/disable` | `\`/scripts/${scriptId}/disable\`` | `web/src/api/index.ts:1847` | [FAIL] |
| POST | `/api/v1/scripts/{var}/enable` | `\`/scripts/${scriptId}/enable\`` | `web/src/api/index.ts:1845` | [FAIL] |
| GET | `/api/v1/scripts/{var}/logs` | `\`/scripts/${scriptId}/logs\`` | `web/src/api/index.ts:1851` | [FAIL] |
| POST | `/api/v1/scripts/{var}/reject` | `\`/scripts/${scriptId}/reject\`` | `web/src/api/index.ts:1858` | [FAIL] |
| POST | `/api/v1/scripts/{var}/submit-review` | `\`/scripts/${scriptId}/submit-review\`` | `web/src/api/index.ts:1854` | [FAIL] |
| POST | `/api/v1/scripts/{var}/test` | `\`/scripts/${scriptId}/test\`` | `web/src/api/index.ts:1849` | [FAIL] |

### 3.42 API `serialBridgeApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| PUT | `/api/v1/serial-bridge/config` | `'/serial-bridge/config'` | `web/src/api/index.ts:696` | [OK] |
| POST | `/api/v1/serial-bridge/start` | `'/serial-bridge/start'` | `web/src/api/index.ts:688` | [OK] |
| GET | `/api/v1/serial-bridge/status` | `'/serial-bridge/status'` | `web/src/api/index.ts:684` | [OK] |
| POST | `/api/v1/serial-bridge/stop` | `'/serial-bridge/stop'` | `web/src/api/index.ts:692` | [OK] |

### 3.43 API `serviceApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/services/list` | `'/services/list'` | `web/src/api/index.ts:864` | [OK] |
| PUT | `/api/v1/services/{var}/config` | `\`/services/${serviceName}/config\`` | `web/src/api/index.ts:891` | [FAIL] |
| POST | `/api/v1/services/{var}/disable` | `\`/services/${serviceName}/disable\`` | `web/src/api/index.ts:875` | [FAIL] |
| POST | `/api/v1/services/{var}/enable` | `\`/services/${serviceName}/enable\`` | `web/src/api/index.ts:871` | [FAIL] |
| POST | `/api/v1/services/{var}/install-deps` | `\`/services/${serviceName}/install-deps\`` | `web/src/api/index.ts:887` | [FAIL] |
| POST | `/api/v1/services/{var}/start` | `\`/services/${serviceName}/start\`` | `web/src/api/index.ts:879` | [FAIL] |
| GET | `/api/v1/services/{var}/status` | `\`/services/${serviceName}/status\`` | `web/src/api/index.ts:867` | [FAIL] |
| POST | `/api/v1/services/{var}/stop` | `\`/services/${serviceName}/stop\`` | `web/src/api/index.ts:883` | [FAIL] |

### 3.44 API `shadowApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/shadows` | `'/shadows'` | `web/src/api/index.ts:1398` | [OK] |
| GET | `/api/v1/shadows/{var}` | `\`/shadows/${deviceId}\`` | `web/src/api/index.ts:1401` | [FAIL] |
| DELETE | `/api/v1/shadows/{var}` | `\`/shadows/${deviceId}\`` | `web/src/api/index.ts:1410` | [FAIL] |
| GET | `/api/v1/shadows/{var}/delta` | `\`/shadows/${deviceId}/delta\`` | `web/src/api/index.ts:1413` | [FAIL] |
| PUT | `/api/v1/shadows/{var}/desired` | `\`/shadows/${deviceId}/desired\`` | `web/src/api/index.ts:1404` | [FAIL] |
| PUT | `/api/v1/shadows/{var}/reported` | `\`/shadows/${deviceId}/reported\`` | `web/src/api/index.ts:1407` | [FAIL] |

### 3.45 API `simulationApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/simulation/assess` | `'/simulation/assess'` | `web/src/api/index.ts:2255` | [OK] |
| POST | `/api/v1/simulation/export` | `'/simulation/export'` | `web/src/api/index.ts:2259` | [OK] |
| POST | `/api/v1/simulation/preview` | `'/simulation/preview'` | `web/src/api/index.ts:2247` | [OK] |
| POST | `/api/v1/simulation/run` | `'/simulation/run'` | `web/src/api/index.ts:2251` | [OK] |
| GET | `/api/v1/simulation/types` | `'/simulation/types'` | `web/src/api/index.ts:2243` | [OK] |

### 3.46 API `systemApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/system/backup` | `'/system/backup'` | `web/src/api/index.ts:490` | [OK] |
| POST | `/api/v1/system/backup` | `'/system/backup'` | `web/src/api/index.ts:493` | [OK] |
| GET | `/api/v1/system/backup/schedule` | `'/system/backup/schedule'` | `web/src/api/index.ts:545` | [OK] |
| PUT | `/api/v1/system/backup/schedule` | `'/system/backup/schedule'` | `web/src/api/index.ts:562` | [FAIL] |
| POST | `/api/v1/system/backup/schedule/trigger` | `'/system/backup/schedule/trigger'` | `web/src/api/index.ts:558` | [OK] |
| DELETE | `/api/v1/system/backup/{var}` | `\`/system/backup/${encodeURIComponent(backupId)}\`` | `web/src/api/index.ts:499` | [FAIL] |
| POST | `/api/v1/system/cascade/config` | `'/system/cascade/config'` | `web/src/api/index.ts:508` | [OK] |
| GET | `/api/v1/system/cascade/neighbors` | `'/system/cascade/neighbors'` | `web/src/api/index.ts:505` | [OK] |
| DELETE | `/api/v1/system/cascade/neighbors/{var}` | `\`/system/cascade/neighbors/${encodeURIComponent(neighborId)}\`` | `web/src/api/index.ts:511` | [FAIL] |
| GET | `/api/v1/system/cascade/topology` | `'/system/cascade/topology'` | `web/src/api/index.ts:502` | [OK] |
| GET | `/api/v1/system/cert` | `'/system/cert'` | `web/src/api/index.ts:529` | [OK] |
| POST | `/api/v1/system/cert/rotate` | `'/system/cert/rotate'` | `web/src/api/index.ts:532` | [OK] |
| GET | `/api/v1/system/config` | `'/system/config'` | `web/src/api/index.ts:566` | [OK] |
| POST | `/api/v1/system/config/reload` | `'/system/config/reload'` | `web/src/api/index.ts:569` | [OK] |
| PUT | `/api/v1/system/config/{var}` | `\`/system/config/${section}\`` | `web/src/api/index.ts:573` | [FAIL] |
| GET | `/api/v1/system/health` | `'/system/health'` | `web/src/api/index.ts:514` | [FAIL] |
| GET | `/api/v1/system/network` | `'/system/network'` | `web/src/api/index.ts:577` | [OK] |
| GET | `/api/v1/system/ntp` | `'/system/ntp'` | `web/src/api/index.ts:535` | [OK] |
| PUT | `/api/v1/system/ntp` | `'/system/ntp'` | `web/src/api/index.ts:538` | [OK] |
| GET | `/api/v1/system/performance` | `'/system/performance'` | `web/src/api/index.ts:520` | [OK] |
| GET | `/api/v1/system/ready` | `'/system/ready'` | `web/src/api/index.ts:517` | [FAIL] |
| GET | `/api/v1/system/resources` | `'/system/resources'` | `web/src/api/index.ts:541` | [OK] |
| POST | `/api/v1/system/restore` | `'/system/restore'` | `web/src/api/index.ts:496` | [OK] |
| GET | `/api/v1/system/retention` | `'/system/retention'` | `web/src/api/index.ts:523` | [OK] |
| PUT | `/api/v1/system/retention` | `'/system/retention'` | `web/src/api/index.ts:526` | [OK] |
| GET | `/api/v1/system/status` | `'/system/status'` | `web/src/api/index.ts:487` | [OK] |

### 3.47 API `templateApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/devices/export` | `'/devices/export'` | `web/src/api/index.ts:1451` | [OK] |
| POST | `/api/v1/devices/from-template` | `'/devices/from-template'` | `web/src/api/index.ts:1445` | [OK] |
| POST | `/api/v1/devices/import` | `'/devices/import'` | `web/src/api/index.ts:1454` | [OK] |
| GET | `/api/v1/devices/templates` | `'/devices/templates'` | `web/src/api/index.ts:1439` | [OK] |
| POST | `/api/v1/devices/templates` | `'/devices/templates'` | `web/src/api/index.ts:1442` | [OK] |
| DELETE | `/api/v1/devices/templates/{var}` | `\`/devices/templates/${encodeURIComponent(name)}\`` | `web/src/api/index.ts:1448` | [FAIL] |

### 3.48 API `thresholdLearnerApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/threshold-learner/dashboard` | `'/threshold-learner/dashboard'` | `web/src/api/index.ts:1284` | [OK] |
| GET | `/api/v1/threshold-learner/decomposition` | `'/threshold-learner/decomposition'` | `web/src/api/index.ts:1286` | [OK] |
| POST | `/api/v1/threshold-learner/feedback` | `'/threshold-learner/feedback'` | `web/src/api/index.ts:1282` | [OK] |
| POST | `/api/v1/threshold-learner/infer` | `'/threshold-learner/infer'` | `web/src/api/index.ts:1280` | [OK] |
| POST | `/api/v1/threshold-learner/initialize` | `'/threshold-learner/initialize'` | `web/src/api/index.ts:1278` | [OK] |

### 3.49 API `trendLearnerApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/trend-learner/dashboard` | `'/trend-learner/dashboard'` | `web/src/api/index.ts:1271` | [OK] |
| POST | `/api/v1/trend-learner/initialize` | `'/trend-learner/initialize'` | `web/src/api/index.ts:1267` | [OK] |
| POST | `/api/v1/trend-learner/predict` | `'/trend-learner/predict'` | `web/src/api/index.ts:1269` | [OK] |
| GET | `/api/v1/trend-learner/residual-analysis` | `'/trend-learner/residual-analysis'` | `web/src/api/index.ts:1273` | [OK] |

### 3.50 API `userApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| GET | `/api/v1/users` | `'/users'` | `web/src/api/index.ts:613` | [OK] |
| POST | `/api/v1/users` | `'/users'` | `web/src/api/index.ts:616` | [OK] |
| PUT | `/api/v1/users/{var}` | `\`/users/${id}\`` | `web/src/api/index.ts:619` | [FAIL] |
| DELETE | `/api/v1/users/{var}` | `\`/users/${id}\`` | `web/src/api/index.ts:623` | [FAIL] |

### 3.51 API `videoApi`

| Method | Path | Raw URL | Source | Backend |
|--------|------|---------|--------|---------|
| POST | `/api/v1/video/webhook` | `'/video/webhook'` | `web/src/api/index.ts:435` | [OK] |
| POST | `/api/v1/video/{var}/ptz` | `\`/video/${deviceId}/ptz\`` | `web/src/api/index.ts:429` | [FAIL] |
| GET | `/api/v1/video/{var}/stream` | `\`/video/${deviceId}/stream\`` | `web/src/api/index.ts:425` | [FAIL] |

---

## 4. WebSocket 路由

### 4.1 后端 WebSocket 路由

| Path | Function | File | Frontend |
|------|----------|------|----------|
| `/api/v1/debug/monitor` | `debug_monitor` | `src/edgelite/api/debug.py` | [WARN] |
| `/ws/v1/realtime` | `ws_realtime` | `src/edgelite/app.py` | [OK] |
| `/ws/v1/alarm` | `ws_alarm` | `src/edgelite/app.py` | [OK] |
| `/ws/v1/device` | `ws_device` | `src/edgelite/app.py` | [OK] |
| `/ws/v1/integration` | `ws_integration` | `src/edgelite/app.py` | [OK] |
| `/ws/v1/ai` | `ws_ai` | `src/edgelite/app.py` | [OK] |

### 4.2 前端 WebSocket 调用

| Path | Source | Backend |
|------|--------|---------|
| `/ws/v1/realtime` | `web/src/api/websocket.ts:1` | [OK] |
| `/ws/v1/alarm` | `web/src/api/websocket.ts:3` | [OK] |
| `/ws/v1/device` | `web/src/api/websocket.ts:6` | [OK] |
| `/ws/v1/integration` | `web/src/api/websocket.ts:6` | [OK] |
| `/ws/v1/ai` | `web/src/api/websocket.ts:8` | [OK] |

---

## 5. 契约不匹配项

### 5.1 404 风险（前端调用但后端无路由）

| Method | Frontend Path | Source | Raw URL |
|--------|---------------|--------|---------|
| GET | `/api/v1/rules/{var}/versions` | `web/src/api/index.ts:296` | ``/rules/${id}/versions`` |
| GET | `/api/v1/rules/{var}/versions/{var}` | `web/src/api/index.ts:298` | ``/rules/${id}/versions/${version}`` |
| POST | `/api/v1/rules/{var}/versions/rollback` | `web/src/api/index.ts:300` | ``/rules/${id}/versions/rollback`` |
| GET | `/api/v1/system/health` | `web/src/api/index.ts:514` | `'/system/health'` |
| GET | `/api/v1/system/ready` | `web/src/api/index.ts:517` | `'/system/ready'` |
| PUT | `/api/v1/system/backup/schedule` | `web/src/api/index.ts:562` | `'/system/backup/schedule'` |
| PUT | `/api/v1/ai/ab-test/{var}/split` | `web/src/api/index.ts:1217` | ``/ai/ab-test/${modelId}/split`` |
| DELETE | `/api/v1/ai/ab-test/{var}` | `web/src/api/index.ts:1219` | ``/ai/ab-test/${modelId}`` |
| GET | `/api/v1/ai/hot-swap/{var}` | `web/src/api/index.ts:1227` | ``/ai/hot-swap/${modelId}`` |
| PUT | `/api/v1/ai/models/{var}/preprocess` | `web/src/api/index.ts:1229` | ``/ai/models/${modelId}/preprocess`` |
| PUT | `/api/v1/ai/models/{var}/postprocess` | `web/src/api/index.ts:1231` | ``/ai/models/${modelId}/postprocess`` |
| PUT | `/api/v1/shadows/{var}/reported` | `web/src/api/index.ts:1407` | ``/shadows/${deviceId}/reported`` |
| DELETE | `/api/v1/shadows/{var}` | `web/src/api/index.ts:1410` | ``/shadows/${deviceId}`` |
| DELETE | `/api/v1/bridge/{var}` | `web/src/api/index.ts:1560` | ``/bridge/${encodeURIComponent(name)}`` |
| POST | `/api/v1/linkage/rules/{var}/enable` | `web/src/api/index.ts:1588` | ``/linkage/rules/${ruleId}/enable`` |
| POST | `/api/v1/linkage/rules/{var}/disable` | `web/src/api/index.ts:1591` | ``/linkage/rules/${ruleId}/disable`` |
| POST | `/api/v1/logs/filters` | `web/src/api/index.ts:1642` | `'/logs/filters'` |
| DELETE | `/api/v1/logs/filters` | `web/src/api/index.ts:1645` | `'/logs/filters'` |
| POST | `/api/v1/logs/level` | `web/src/api/index.ts:1648` | `'/logs/level'` |
| DELETE | `/api/v1/config/versions/{var}` | `web/src/api/index.ts:1716` | ``/config/versions/${versionId}`` |
| POST | `/api/v1/resource-shares/transfer` | `web/src/api/index.ts:1830` | `URL.RESOURCE_SHARES.TRANSFER` |
| DELETE | `/api/v1/scripts/{var}` | `web/src/api/index.ts:1843` | ``/scripts/${scriptId}`` |
| POST | `/api/v1/observability/alerts/rules` | `web/src/api/index.ts:2114` | `'/observability/alerts/rules'` |
| PUT | `/api/v1/observability/alerts/rules/{var}` | `web/src/api/index.ts:2117` | ``/observability/alerts/rules/${encodeURIComponent(ruleName)}`` |
| DELETE | `/api/v1/observability/alerts/rules/{var}` | `web/src/api/index.ts:2120` | ``/observability/alerts/rules/${encodeURIComponent(ruleName)}`` |

### 5.2 Dead Code 警告（后端有路由但前端未调用）

> 注意：dead code 仅是 warning，不阻断 CI。某些端点可能由外部系统调用或保留以备未来使用。

| Method | Backend Path | Module | Function |
|--------|--------------|--------|----------|
| GET | `/api/v1/ai/ab-test` | `ai_models` | `list_ab_tests` |
| POST | `/api/v1/ai/ab-test/{test_id}/split` | `ai_models` | `update_ab_test_split` |
| GET | `/api/v1/ai/hot-swap` | `ai_models` | `list_hot_swaps` |
| POST | `/api/v1/ai/models/{model_id}/preprocess` | `ai_models` | `set_preprocess` |
| GET | `/api/v1/ai/models/{model_id}/preprocess` | `ai_models` | `get_preprocess` |
| POST | `/api/v1/ai/models/{model_id}/postprocess` | `ai_models` | `set_postprocess` |
| GET | `/api/v1/ai/models/{model_id}/postprocess` | `ai_models` | `get_postprocess` |
| DELETE | `/api/v1/alarms/{alarm_id}` | `alarms` | `delete_alarm` |
| POST | `/api/v1/auth/forgot-password` | `auth` | `forgot_password` |
| POST | `/api/v1/auth/reset-password` | `auth` | `reset_password` |
| POST | `/api/v1/debug/read` | `debug` | `debug_read` |
| POST | `/api/v1/debug/write` | `debug` | `debug_write` |
| POST | `/api/v1/devices/batch/delete` | `devices` | `batch_delete_devices` |
| POST | `/api/v1/devices/{device_id}/probe-primary` | `devices` | `probe_primary_link` |
| GET | `/api/v1/devices/{device_id}/metrics` | `devices` | `get_device_metrics` |
| GET | `/api/v1/devices/{device_id}/config-versions` | `devices` | `list_config_versions` |
| GET | `/api/v1/devices/{device_id}/config-versions/current` | `devices` | `get_config_current` |
| GET | `/api/v1/devices/{device_id}/config-versions/{version}` | `devices` | `get_config_version_detail` |
| POST | `/api/v1/devices/{device_id}/config-versions` | `devices` | `save_config_version` |
| POST | `/api/v1/devices/{device_id}/config-versions/rollback` | `devices` | `rollback_config_version` |
| GET | `/api/v1/devices/{device_id}/config-versions/audit` | `devices` | `get_config_audit_trail` |
| GET | `/api/v1/devices/{device_id}/config-versions/diff` | `devices` | `diff_config_versions` |
| GET | `/api/v1/drivers/{driver_name}/environment-check` | `drivers` | `driver_environment_check` |
| GET | `/api/v1/drivers/opcua/certificate-status` | `drivers` | `get_opcua_certificate_status` |
| GET | `/api/v1/drivers/health` | `drivers` | `get_all_drivers_health` |
| POST | `/api/v1/integration/push-device` | `integration` | `push_device` |
| GET | `/api/v1/integration/health` | `integration` | `integration_health_check` |
| GET | `/api/v1/logs/filters` | `log_aggregation` | `get_filters` |
| PUT | `/api/v1/logs/level` | `log_aggregation` | `set_log_level` |
| GET | `/api/v1/mcp/sse-ticket` | `mcp` | `create_sse_ticket` |
| GET | `/api/v1/mcp/sse` | `mcp` | `mcp_sse` |
| GET | `/api/v1/resource-shares` | `resource_shares` | `list_my_shares` |
| POST | `/api/v1/resource-shares/check` | `resource_shares` | `check_access` |
| POST | `/api/v1/rules/test` | `rules` | `test_rule_definition` |
| POST | `/api/v1/rules/batch/delete` | `rules` | `batch_delete_rules` |
| POST | `/api/v1/rules/batch/enable` | `rules` | `batch_enable_rules` |
| POST | `/api/v1/rules/batch/disable` | `rules` | `batch_disable_rules` |
| GET | `/api/v1/scripts/{script_id}` | `scripts` | `get_script` |
| POST | `/api/v1/shadows/{device_id}/reported` | `shadow` | `update_reported` |
| GET | `/api/v1/system/quality/{device_id}` | `system` | `get_device_quality` |
| GET | `/api/v1/system/circuit-breakers` | `system` | `get_circuit_breaker_status` |
| POST | `/api/v1/system/circuit-breakers/{device_id}/reset` | `system` | `reset_circuit_breaker` |
| GET | `/api/v1/system/health/basic` | `system` | `health_check_basic` |
| GET | `/api/v1/system/ready-status` | `system` | `readiness_check_api` |
| GET | `/api/v1/system/migration/status` | `system` | `get_migration_status` |
| POST | `/api/v1/system/migration/retry` | `system` | `retry_migration` |
| GET | `/api/v1/system/migration/history` | `system` | `get_migration_history` |
| GET | `/api/v1/system/locks/status` | `system` | `get_lock_status` |
| GET | `/api/v1/users/{user_id}` | `users` | `get_user` |

### 5.3 未定义 URL 常量

[PASS] 前端所有 URL 常量引用都已定义。

---

## 6. CI 集成

项目已集成 `scripts/check_api_contract.py` 作为 CI 校验步骤：

- **GitHub Actions**：`.github/workflows/ci.yml` 的 `lint` job 中运行 `python scripts/check_api_contract.py`
- **GitLab CI**：`.gitlab-ci.yml` 的 `lint` stage 中运行 `python scripts/check_api_contract.py`

**退出码**：
- `0` 契约对齐（或仅有 dead code warning）
- `1` 存在 404 风险或未定义 URL 常量

**重新生成本文档**：

```bash
python scripts/check_api_contract.py --json > scripts/contract_report.json
python scripts/gen_api_contract_doc.py
```
