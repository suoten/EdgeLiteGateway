# EdgeLite v1.0 社区版 — 表单 ↔ API ↔ 数据库 对照表

> 生成日期: 2026-06-09 | 覆盖范围: 前端表单字段 → Pydantic请求模型 → SQLAlchemy ORM列

---

## 1. 登录页 (`Login.vue`)

### 1.1 登录表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 登录 | form.username | n-input | LoginRequest.username | str (1-32) | UserORM.username | VARCHAR(32) UNIQUE |
| 登录 | form.password | n-input(password) | LoginRequest.password | str (1-72) | UserORM.password | VARCHAR(128) |

### 1.2 修改密码表单（首次登录强制修改）

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 登录 | changePwdForm.old_password | n-input(password) | change-password.old_password | str | UserORM.password | VARCHAR(128) |
| 登录 | changePwdForm.new_password | n-input(password) | change-password.new_password | str (8-72, 含字母+数字) | UserORM.password | VARCHAR(128) |
| 登录 | changePwdForm.confirm_password | n-input(password) | — (前端校验) | — | — | — |

---

## 2. 设备管理 (`DeviceList.vue`)

### 2.1 创建设备表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 设备管理 | createForm.device_id | n-input | DeviceCreate.device_id | str (regex: `^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$`) | DeviceORM.device_id | VARCHAR(64) PK |
| 设备管理 | createForm.name | n-input | DeviceCreate.name | str (1-64) | DeviceORM.name | VARCHAR(64) NOT NULL |
| 设备管理 | createForm.protocol | n-select | DeviceCreate.protocol | str (14种协议) | DeviceORM.protocol | VARCHAR(32) NOT NULL |
| 设备管理 | createForm.collect_interval | n-input-number(1-3600) | DeviceCreate.collect_interval | int (≥1, default=5) | DeviceORM.collect_interval | INTEGER NOT NULL DEFAULT 5 |
| 设备管理 | (protocolFormRef) | 动态协议组件 | DeviceCreate.config | dict[str, Any] | DeviceORM.config | TEXT NOT NULL DEFAULT '{}' |
| 设备管理 | (protocolFormRef) | 动态协议组件 | DeviceCreate.points | list[PointDef] (≥1) | DeviceORM.points | TEXT NOT NULL DEFAULT '[]' |

### 2.2 编辑设备表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 设备管理 | editForm.device_id | n-input(disabled) | — (路径参数) | — | DeviceORM.device_id | VARCHAR(64) PK |
| 设备管理 | editForm.name | n-input | DeviceUpdate.name | str\|None (1-64) | DeviceORM.name | VARCHAR(64) NOT NULL |
| 设备管理 | editForm.collect_interval | n-input-number | DeviceUpdate.collect_interval | int\|None (≥1) | DeviceORM.collect_interval | INTEGER NOT NULL DEFAULT 5 |
| 设备管理 | (protocolEditRef) | 动态协议组件 | DeviceUpdate.config | dict[str, Any]\|None | DeviceORM.config | TEXT NOT NULL DEFAULT '{}' |
| 设备管理 | (protocolEditRef) | 动态协议组件 | DeviceUpdate.points | list[PointDef]\|None | DeviceORM.points | TEXT NOT NULL DEFAULT '[]' |

### 2.3 创建模拟设备表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 设备管理 | simForm.device_id | n-input | SimulatorCreate.device_id | str (regex同上) | DeviceORM.device_id | VARCHAR(64) PK |
| 设备管理 | simForm.name | n-input | SimulatorCreate.name | str (1-64) | DeviceORM.name | VARCHAR(64) NOT NULL |
| 设备管理 | simForm.collect_interval | n-input-number | SimulatorCreate.collect_interval | int (≥1, default=5) | DeviceORM.collect_interval | INTEGER NOT NULL DEFAULT 5 |
| 设备管理 | simForm.points | 动态测点列表 | SimulatorCreate.points | list[PointDef] (≥1) | DeviceORM.points | TEXT NOT NULL DEFAULT '[]' |

### 2.4 测点定义 (PointDef) — 嵌套于设备表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 设备管理 | pt.name | n-input | PointDef.name | str | (points JSON内) | — |
| 设备管理 | pt.data_type | n-select(8种) | PointDef.data_type | Literal["bool","int16","int32","uint16","uint32","float32","float64","string"] | (points JSON内) | — |
| 设备管理 | pt.unit | n-input | PointDef.unit | str (default="") | (points JSON内) | — |
| 设备管理 | pt.address | — (协议组件内) | PointDef.address | str (default="0") | (points JSON内) | — |
| 设备管理 | pt.access_mode | — (协议组件内) | PointDef.access_mode | Literal["r","w","rw"] (default="r") | (points JSON内) | — |
| 设备管理 | pt.min | n-input-number | PointDef.min | float\|None | (points JSON内) | — |
| 设备管理 | pt.max | n-input-number | PointDef.max | float\|None | (points JSON内) | — |
| 设备管理 | pt.mode | n-select | PointDef.mode | str\|None | (points JSON内) | — |

### 2.5 设备发现表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 设备管理 | discoverProtocol | n-select | DiscoverRequest.protocol | str (default="modbus_tcp") | — | — |
| 设备管理 | discoverHost | n-input | DiscoverRequest.config.host | dict内 | — | — |
| 设备管理 | discoverPort | n-input-number | DiscoverRequest.config.port | dict内 | — | — |

### 2.6 设备模板

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 设备管理 | template_name | n-input | TemplateCreate.template_name | str (1-64) | DeviceTemplateORM.name | VARCHAR(64) PK |
| 设备管理 | (从设备创建) | — | TemplateCreate.device_id | str | DeviceTemplateORM.* | — |
| 设备管理 | template_name | n-select | CreateFromTemplateRequest.template_name | str | — | — |
| 设备管理 | device_id | n-input | CreateFromTemplateRequest.device_id | str (regex同上) | DeviceORM.device_id | VARCHAR(64) PK |
| 设备管理 | name | n-input | CreateFromTemplateRequest.name | str (1-64) | DeviceORM.name | VARCHAR(64) NOT NULL |
| 设备管理 | config | — | CreateFromTemplateRequest.config | dict[str, Any]\|None | DeviceORM.config | TEXT |
| 设备管理 | collect_interval | n-input-number | CreateFromTemplateRequest.collect_interval | int (≥1, default=5) | DeviceORM.collect_interval | INTEGER |

### 2.7 设备导入

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 设备管理 | importAtomicMode | n-checkbox | ImportDevicesRequest.atomic | bool (default=False) | — | — |

### 2.8 资源分享表单（设备/规则共用）

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 设备管理 | shareForm.shared_with_user_id | n-select | shared_with_user_id | str | ResourceShareORM.shared_with_user_id | VARCHAR(64) NOT NULL |
| 设备管理 | shareForm.permission_level | n-select(read/write) | permission_level | str | ResourceShareORM.permission_level | VARCHAR(16) NOT NULL DEFAULT 'read' |

### 2.9 所有权转移表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 设备管理 | transferForm.new_owner_id | n-select | new_owner_id | str | DeviceORM.created_by | VARCHAR(64) |

---

## 3. 规则管理 (`RuleList.vue`)

### 3.1 创建/编辑规则表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 规则管理 | createForm.name | n-input | RuleCreate.name | str (1-64) | RuleORM.name | VARCHAR(64) NOT NULL |
| 规则管理 | createForm.device_id | n-select(filterable) | RuleCreate.device_id | str | RuleORM.device_id | VARCHAR(64) NULL |
| 规则管理 | createForm.logic | n-radio-group(AND/OR/NOT) | RuleCreate.logic | Literal["AND","OR","NOT"] (default="AND") | RuleORM.logic | VARCHAR(8) NOT NULL DEFAULT 'AND' |
| 规则管理 | createForm.duration | n-input-number(0-3600) | RuleCreate.duration | int (0-3600, default=0) | RuleORM.duration | INTEGER NOT NULL DEFAULT 0 |
| 规则管理 | createForm.severity | n-select(5级) | RuleCreate.severity | Literal["critical","major","warning","minor","info"] | RuleORM.severity | VARCHAR(16) NOT NULL |
| 规则管理 | createForm.notify_channels | n-select(multiple,4种) | RuleCreate.notify_channels | list[Literal["dingtalk","email","wechat","webhook"]] | RuleORM.notify_channels | TEXT NOT NULL DEFAULT '[]' |
| 规则管理 | createForm.conditions | 动态条件列表 | RuleCreate.conditions | list[RuleCondition] (≥1) | RuleORM.conditions | TEXT NOT NULL DEFAULT '[]' |

### 3.2 规则条件 (RuleCondition) — 嵌套于规则表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 规则管理 | cond.point | n-select(filterable) | RuleCondition.point | str | (conditions JSON内) | — |
| 规则管理 | cond.operator | n-select(5种) | RuleCondition.operator | Literal[">",">=","<","<=","=="] | (conditions JSON内) | — |
| 规则管理 | cond.threshold | n-input-number | RuleCondition.threshold | float | (conditions JSON内) | — |
| 规则管理 | — | — | RuleCondition.type | Literal["threshold","ai_inference"] (default="threshold") | (conditions JSON内) | — |
| 规则管理 | — | — | RuleCondition.model_id | str\|None | (conditions JSON内) | — |
| 规则管理 | — | — | RuleCondition.ai_threshold | float\|None | (conditions JSON内) | — |

### 3.3 规则测试表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 规则管理 | testPointValues[pt] | n-input-number | RuleTestRequest.point_values | dict[str, float] | — | — |

---

## 4. 告警管理 (`AlarmList.vue`)

### 4.1 告警列表（只读展示，无创建表单）

| 页面 | 展示字段 | 对应 API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|-------------|--------------|---------|--------|
| 告警管理 | alarm_id | AlarmResponse.alarm_id | str | AlarmORM.alarm_id | VARCHAR(16) PK |
| 告警管理 | rule_id | AlarmResponse.rule_id | str | AlarmORM.rule_id | VARCHAR(16) NOT NULL |
| 告警管理 | device_id | AlarmResponse.device_id | str\|None | AlarmORM.device_id | VARCHAR(64) NULL |
| 告警管理 | severity | AlarmResponse.severity | str | AlarmORM.severity | VARCHAR(16) NOT NULL |
| 告警管理 | status | AlarmResponse.status | str | AlarmORM.status | VARCHAR(16) NOT NULL DEFAULT 'firing' |
| 告警管理 | message | AlarmResponse.message | str (default="") | AlarmORM.message | VARCHAR(256) NOT NULL DEFAULT '' |
| 告警管理 | trigger_value | AlarmResponse.trigger_value | dict[str, Any] | AlarmORM.trigger_value | TEXT NOT NULL DEFAULT '{}' |
| 告警管理 | trigger_count | AlarmResponse.trigger_count | int | AlarmORM.trigger_count | INTEGER NOT NULL DEFAULT 1 |
| 告警管理 | fired_at | AlarmResponse.fired_at | str | AlarmORM.fired_at | DATETIME |
| 告警管理 | acknowledged_at | AlarmResponse.acknowledged_at | str\|None | AlarmORM.acknowledged_at | DATETIME NULL |
| 告警管理 | acknowledged_by | AlarmResponse.acknowledged_by | str\|None | AlarmORM.acknowledged_by | VARCHAR(64) NULL |
| 告警管理 | recovered_at | AlarmResponse.recovered_at | str\|None | AlarmORM.recovered_at | DATETIME NULL |
| 告警管理 | rule_type | AlarmResponse.rule_type | str (default="threshold") | AlarmORM.rule_type | VARCHAR(32) NOT NULL DEFAULT 'threshold' |

### 4.2 告警静默期表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 告警管理 | silenceForm.device_id | n-select(clearable) | SilenceCreateRequest.device_id | str | AlarmSilenceORM.device_id | VARCHAR(64) NOT NULL DEFAULT '' |
| 告警管理 | silenceForm.rule_id | n-select(clearable) | SilenceCreateRequest.rule_id | str | AlarmSilenceORM.rule_id | VARCHAR(16) NOT NULL DEFAULT '' |
| 告警管理 | silenceForm.start_time | n-date-picker(datetime) | SilenceCreateRequest.start_time | datetime | AlarmSilenceORM.start_time | DATETIME NOT NULL |
| 告警管理 | silenceForm.end_time | n-date-picker(datetime) | SilenceCreateRequest.end_time | datetime | AlarmSilenceORM.end_time | DATETIME NOT NULL |
| 告警管理 | silenceForm.reason | n-input(textarea) | SilenceCreateRequest.reason | str | AlarmSilenceORM.reason | VARCHAR(256) NOT NULL DEFAULT '' |

### 4.3 告警抑制表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 告警管理 | suppressForm.duration_seconds | n-radio-group(1h/4h/24h) | SuppressRequest.duration_seconds | int | — | — |
| 告警管理 | suppressForm.reason | n-input(textarea) | SuppressRequest.reason | str | — | — |

---

## 5. 用户管理 (`UserManage.vue`)

### 5.1 创建用户表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 用户管理 | createForm.username | n-input | UserCreate.username | str (3-32, regex: `^[a-zA-Z0-9_]+$`) | UserORM.username | VARCHAR(32) NOT NULL UNIQUE |
| 用户管理 | createForm.password | n-input(password) | UserCreate.password | str (8-72, 含字母+数字) | UserORM.password | VARCHAR(128) NOT NULL |
| 用户管理 | createForm.role | n-select(admin/operator/viewer) | UserCreate.role | Literal["admin","operator","viewer"] | UserORM.role | VARCHAR(16) NOT NULL |

### 5.2 编辑用户表单

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 用户管理 | editForm.username | n-input(disabled) | — | — | UserORM.username | VARCHAR(32) NOT NULL UNIQUE |
| 用户管理 | editForm.role | n-select | UserUpdate.role | Literal["admin","operator","viewer"]\|None | UserORM.role | VARCHAR(16) NOT NULL |
| 用户管理 | editForm.password | n-input(password) | UserUpdate.password | str\|None (8-72, 含字母+数字) | UserORM.password | VARCHAR(128) NOT NULL |
| 用户管理 | editForm.enabled | n-switch | UserUpdate.enabled | bool\|None | UserORM.enabled | BOOLEAN NOT NULL DEFAULT 1 |

---

## 6. AI模型管理 (`AiModel.vue` / `AiCenter.vue`)

### 6.1 AI模型创建/更新

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| AI模型 | model_name | n-input | AiModelCreate.model_name | str (1-128) | AiModelORM.model_name | VARCHAR(128) NOT NULL |
| AI模型 | model_version | n-input | AiModelCreate.model_version | str (regex: `^v\d+\.\d+\.\d+$`) | AiModelORM.model_version | VARCHAR(16) NOT NULL |
| AI模型 | model_type | n-select | AiModelCreate.model_type | ModelType (anomaly/trend/threshold/custom) | AiModelORM.model_type | VARCHAR(16) NOT NULL |
| AI模型 | model_file_path | n-input | AiModelCreate.model_file_path | str (regex: `.+\.onnx$`) | AiModelORM.model_file_path | VARCHAR(256) NOT NULL |
| AI模型 | input_schema | — (JSON编辑) | AiModelCreate.input_schema | dict | AiModelORM.input_schema | TEXT NOT NULL DEFAULT '{}' |
| AI模型 | output_schema | — (JSON编辑) | AiModelCreate.output_schema | dict | AiModelORM.output_schema | TEXT NOT NULL DEFAULT '{}' |
| AI模型 | is_preset | — | AiModelCreate.is_preset | bool (default=False) | AiModelORM.is_preset | BOOLEAN DEFAULT 0 |
| AI模型 | preprocess_config | — | AiModelCreate.preprocess_config | list[dict] | — | — |
| AI模型 | postprocess_config | — | AiModelCreate.postprocess_config | list[dict] | — | — |
| AI模型 | batch_size | n-input-number | AiModelCreate.batch_size | int (1-128, default=1) | — | — |
| AI模型 | max_concurrent | n-input-number | AiModelCreate.max_concurrent | int (1-32, default=4) | — | — |
| AI模型 | timeout_ms | n-input-number | AiModelCreate.timeout_ms | int (100-300000, default=30000) | — | — |
| AI模型 | device_preference | n-select | AiModelCreate.device_preference | str (auto/cpu/cuda/directml/openvino) | — | — |

### 6.2 AI模型热加载

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| AI模型 | model_file_path | n-input | AiModelReloadRequest.model_file_path | str (regex: `.+\.onnx$`) | AiModelORM.model_file_path | VARCHAR(256) |

### 6.3 定时推理

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| AI模型 | device_id | n-select | ScheduleInferenceRequest.device_id | str | — | — |
| AI模型 | point_name | n-input | ScheduleInferenceRequest.point_name | str | — | — |
| AI模型 | interval_seconds | n-input-number | ScheduleInferenceRequest.interval_seconds | int (5-3600, default=60) | — | — |
| AI模型 | input_window_size | n-input-number | ScheduleInferenceRequest.input_window_size | int (1-10000, default=100) | — | — |

### 6.4 手动推理

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| AI模型 | model_id | n-select | AiInferenceRequest.model_id | str | AiInferenceLogORM.model_id | VARCHAR(36) |
| AI模型 | input_data | — | AiInferenceRequest.input_data | list[float] | — | — |
| AI模型 | device_id | n-select | AiInferenceRequest.device_id | str\|None | AiInferenceLogORM.device_id | VARCHAR(64) NULL |
| AI模型 | point_name | n-input | AiInferenceRequest.point_name | str\|None | AiInferenceLogORM.point_name | VARCHAR(64) NULL |

---

## 7. 通知配置 (`NotifyConfig.vue`)

> 通知配置存储在 `configs/config.yaml`，不直接对应数据库表，通过 `/system/config` API 读写。

### 7.1 钉钉通知

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 通知配置 | dingtalkForm.webhook_url | n-input | dingtalk.webhook_url | str | — (YAML配置) | — |
| 通知配置 | dingtalkForm.secret | n-input(password) | dingtalk.secret | str | — (YAML配置) | — |
| 通知配置 | dingtalkForm.at_mobiles | n-dynamic-tags | dingtalk.at_mobiles | list[str] | — (YAML配置) | — |
| 通知配置 | dingtalkForm.is_at_all | n-switch | dingtalk.is_at_all | bool | — (YAML配置) | — |
| 通知配置 | dingtalkForm.max_per_minute | n-input-number(1-100) | dingtalk.max_per_minute | int | — (YAML配置) | — |
| 通知配置 | dingtalkForm.cooldown_seconds | n-input-number(0-3600) | dingtalk.cooldown_seconds | int | — (YAML配置) | — |

### 7.2 企业微信通知

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 通知配置 | wecomForm.webhook_url | n-input | wecom.webhook_url | str | — (YAML配置) | — |
| 通知配置 | wecomForm.max_per_minute | n-input-number | wecom.max_per_minute | int | — (YAML配置) | — |
| 通知配置 | wecomForm.cooldown_seconds | n-input-number | wecom.cooldown_seconds | int | — (YAML配置) | — |

### 7.3 邮件通知

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 通知配置 | emailForm.smtp_host | n-input | email.smtp_host | str | — (YAML配置) | — |
| 通知配置 | emailForm.smtp_port | n-input-number(1-65535) | email.smtp_port | int | — (YAML配置) | — |
| 通知配置 | emailForm.smtp_user | n-input | email.smtp_user | str | — (YAML配置) | — |
| 通知配置 | emailForm.smtp_password | n-input(password) | email.smtp_password | str | — (YAML配置) | — |
| 通知配置 | emailForm.from_address | n-input | email.from_address | str | — (YAML配置) | — |
| 通知配置 | emailForm.to_addresses | n-dynamic-tags | email.to_addresses | list[str] | — (YAML配置) | — |
| 通知配置 | emailForm.use_tls | n-switch | email.use_tls | bool | — (YAML配置) | — |
| 通知配置 | emailForm.use_ssl | n-switch | email.use_ssl | bool | — (YAML配置) | — |
| 通知配置 | emailForm.max_per_minute | n-input-number | email.max_per_minute | int | — (YAML配置) | — |
| 通知配置 | emailForm.cooldown_seconds | n-input-number | email.cooldown_seconds | int | — (YAML配置) | — |

### 7.4 Webhook通知

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 通知配置 | webhookForm.url | n-input | webhook.url | str | — (YAML配置) | — |
| 通知配置 | webhookForm.method | n-select(POST/PUT) | webhook.method | str | — (YAML配置) | — |
| 通知配置 | headersText | n-input(textarea) | webhook.headers | dict | — (YAML配置) | — |
| 通知配置 | webhookForm.auth_type | n-select(none/basic/bearer/api_key) | webhook.auth_type | str | — (YAML配置) | — |
| 通知配置 | webhookForm.auth_token | n-input(password) | webhook.auth_token | str | — (YAML配置) | — |
| 通知配置 | webhookForm.auth_username | n-input | webhook.auth_username | str | — (YAML配置) | — |
| 通知配置 | webhookForm.auth_password | n-input(password) | webhook.auth_password | str | — (YAML配置) | — |
| 通知配置 | webhookForm.max_per_minute | n-input-number | webhook.max_per_minute | int | — (YAML配置) | — |
| 通知配置 | webhookForm.cooldown_seconds | n-input-number | webhook.cooldown_seconds | int | — (YAML配置) | — |

---

## 8. 平台配置 (`PlatformConfig.vue`)

> 平台配置由后端 `config_schema` 动态驱动，存储在 `configs/config.yaml`，不直接对应数据库表。

### 8.1 北向平台基础配置

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 平台配置 | server_url | n-input | NorthConfig.connection_params.server_url | str | — (YAML配置) | — |
| 平台配置 | mqtt_host | n-input | MqttConnectionConfig.broker_host | str (default="") | — (YAML配置) | — |
| 平台配置 | mqtt_port | n-input-number | MqttConnectionConfig.broker_port | int (default=1883) | — (YAML配置) | — |
| 平台配置 | access_token | n-input(password) | NorthConfig.connection_params.access_token | str | — (YAML配置) | — |
| 平台配置 | username | n-input | MqttConnectionConfig.username | str (default="") | — (YAML配置) | — |
| 平台配置 | password | n-input(password) | MqttConnectionConfig.password | str (default="") | — (YAML配置) | — |
| 平台配置 | client_id | n-input | MqttConnectionConfig.client_id | str (default="") | — (YAML配置) | — |
| 平台配置 | keepalive | n-input-number | MqttConnectionConfig.keepalive | int (default=60) | — (YAML配置) | — |
| 平台配置 | protocol_version | n-select(4/5) | MqttConnectionConfig.protocol_version | int (default=4) | — (YAML配置) | — |
| 平台配置 | transport | n-select(tcp/websockets) | MqttConnectionConfig.transport | str (default="tcp") | — (YAML配置) | — |
| 平台配置 | clean_session | n-switch | MqttConnectionConfig.clean_session | bool (default=True) | — (YAML配置) | — |

### 8.2 TLS/SSL配置

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 平台配置 | tls.enabled | n-switch | MqttTlsConfig.enabled | bool (default=False) | — (YAML配置) | — |
| 平台配置 | tls.verify_server | n-switch | MqttTlsConfig.verify_server | bool (default=True) | — (YAML配置) | — |
| 平台配置 | tls.ca_cert | n-input(textarea) | MqttTlsConfig.ca_cert | str (default="") | — (YAML配置) | — |
| 平台配置 | tls.client_cert | n-input(textarea) | MqttTlsConfig.client_cert | str (default="") | — (YAML配置) | — |
| 平台配置 | tls.client_key | n-input(textarea) | MqttTlsConfig.client_key | str (default="") | — (YAML配置) | — |

### 8.3 遗嘱消息配置

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 平台配置 | will.enabled | n-switch | MqttWillConfig.enabled | bool (default=False) | — (YAML配置) | — |
| 平台配置 | will.topic | n-input | MqttWillConfig.topic | str (default="") | — (YAML配置) | — |
| 平台配置 | will.payload | n-input | MqttWillConfig.payload | str (default="") | — (YAML配置) | — |
| 平台配置 | will.qos | n-select | MqttWillConfig.qos | int (default=1) | — (YAML配置) | — |
| 平台配置 | will.retain | n-switch | MqttWillConfig.retain | bool (default=True) | — (YAML配置) | — |

### 8.4 MQTT 5.0属性

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 平台配置 | mqtt5.message_expiry_interval | n-input-number | Mqtt5Properties.message_expiry_interval | int\|None | — (YAML配置) | — |
| 平台配置 | mqtt5.content_type | n-input | Mqtt5Properties.content_type | str (default="") | — (YAML配置) | — |

### 8.5 主题模板配置

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 平台配置 | topic_template | n-input | TopicTemplateConfig.template | str (default="{prefix}/{device_id}/{point_id}") | — (YAML配置) | — |
| 平台配置 | status_template | n-input | TopicTemplateConfig.status_template | str (default="{prefix}/{device_id}/status") | — (YAML配置) | — |
| 平台配置 | prefix | n-input | TopicTemplateConfig.prefix | str (default="edgelite") | — (YAML配置) | — |

### 8.6 负载格式与QoS

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 平台配置 | payload_format | n-select(json/cbor/protobuf/custom) | PayloadConfig.format | str (default="json") | — (YAML配置) | — |
| 平台配置 | custom_template | n-input(textarea) | PayloadConfig.custom_template | str (default="") | — (YAML配置) | — |
| 平台配置 | default_qos | n-select | QosPolicy.default_qos | int (default=0) | — (YAML配置) | — |
| 平台配置 | alarm_qos | n-select | QosPolicy.alarm_qos | int (default=1) | — (YAML配置) | — |
| 平台配置 | dedup_window_seconds | n-input-number | NorthConfig.dedup_window_seconds | int (default=300) | — (YAML配置) | — |

---

## 9. 自定义MQTT配置 (`CustomMqttConfig.vue`)

> 存储在 `configs/config.yaml`，不直接对应数据库表。

### 9.1 Broker配置（可多条）

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 自定义MQTT | broker.name | n-input | CustomMqttBrokerConfig.name | str (default="default") | — (YAML配置) | — |
| 自定义MQTT | broker.broker_host | n-input | CustomMqttBrokerConfig.broker_host | str (default="") | — (YAML配置) | — |
| 自定义MQTT | broker.broker_port | n-input-number(1-65535) | CustomMqttBrokerConfig.broker_port | int (default=1883) | — (YAML配置) | — |
| 自定义MQTT | broker.username | n-input | CustomMqttBrokerConfig.username | str (default="") | — (YAML配置) | — |
| 自定义MQTT | broker.password | n-input(password) | CustomMqttBrokerConfig.password | str (default="") | — (YAML配置) | — |
| 自定义MQTT | broker.client_id | n-input | CustomMqttBrokerConfig.client_id | str (default="") | — (YAML配置) | — |
| 自定义MQTT | broker.protocol_version | n-select(4/5) | CustomMqttBrokerConfig.protocol_version | int (default=4) | — (YAML配置) | — |
| 自定义MQTT | broker.transport | n-select(tcp/websockets) | CustomMqttBrokerConfig.transport | str (default="tcp") | — (YAML配置) | — |
| 自定义MQTT | broker.keepalive | n-input-number | CustomMqttBrokerConfig.keepalive | int (default=60) | — (YAML配置) | — |
| 自定义MQTT | broker.enabled | n-switch | CustomMqttBrokerConfig.enabled | bool (default=True) | — (YAML配置) | — |
| 自定义MQTT | broker.tls_enabled | n-switch | MqttTlsConfig.enabled | bool (default=False) | — (YAML配置) | — |
| 自定义MQTT | broker.verify_server | n-switch | MqttTlsConfig.verify_server | bool (default=True) | — (YAML配置) | — |
| 自定义MQTT | broker.ca_cert | n-input(textarea) | MqttTlsConfig.ca_cert | str (default="") | — (YAML配置) | — |
| 自定义MQTT | broker.client_cert | n-input(textarea) | MqttTlsConfig.client_cert | str (default="") | — (YAML配置) | — |
| 自定义MQTT | broker.client_key | n-input(textarea) | MqttTlsConfig.client_key | str (default="") | — (YAML配置) | — |

### 9.2 模板配置

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 自定义MQTT | templateConfig.topic_template | n-input | CustomMqttTemplateConfig.topic_template | str | — (YAML配置) | — |
| 自定义MQTT | templateConfig.payload_template | n-input(textarea) | CustomMqttTemplateConfig.payload_template | str | — (YAML配置) | — |
| 自定义MQTT | templateConfig.batch_payload_template | n-input(textarea) | CustomMqttTemplateConfig.batch_payload_template | str | — (YAML配置) | — |
| 自定义MQTT | templateConfig.topic_prefix | n-input | CustomMqttTemplateConfig.topic_prefix | str | — (YAML配置) | — |

### 9.3 脚本配置

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 自定义MQTT | scriptConfig.enabled | n-switch | CustomMqttScriptConfig.enabled | bool (default=False) | — (YAML配置) | — |
| 自定义MQTT | scriptConfig.script | n-input(textarea) | CustomMqttScriptConfig.script | str (default="") | — (YAML配置) | — |
| 自定义MQTT | scriptConfig.script_language | n-select | CustomMqttScriptConfig.script_language | str (default="javascript") | — (YAML配置) | — |

### 9.4 高级配置

| 页面 | 表单字段 | 前端类型 | API 字段 | Pydantic 类型 | DB 字段 | DB 类型 |
|------|---------|---------|---------|--------------|---------|--------|
| 自定义MQTT | advancedConfig.payload_format | n-select | CustomMqttFullConfig.payload_format | str (default="custom") | — (YAML配置) | — |
| 自定义MQTT | advancedConfig.enable_compression | n-switch | CustomMqttFullConfig.enable_compression | bool (default=True) | — (YAML配置) | — |
| 自定义MQTT | advancedConfig.compress_threshold | n-input-number | CustomMqttFullConfig.compress_threshold | int (default=1024) | — (YAML配置) | — |
| 自定义MQTT | advancedConfig.dedup_window_seconds | n-input-number | CustomMqttFullConfig.dedup_window_seconds | int (default=300) | — (YAML配置) | — |
| 自定义MQTT | advancedConfig.default_qos | n-select | QosPolicy.default_qos | int (default=0) | — (YAML配置) | — |
| 自定义MQTT | advancedConfig.alarm_qos | n-select | QosPolicy.alarm_qos | int (default=1) | — (YAML配置) | — |

---

## 10. 系统自动生成字段（无前端表单）

以下DB字段由后端自动生成/管理，前端不直接编辑：

### 10.1 设备 (DeviceORM)

| DB 字段 | DB 类型 | 说明 |
|---------|--------|------|
| DeviceORM.status | VARCHAR(16) DEFAULT 'offline' | 引擎自动维护(online/offline/error/unknown) |
| DeviceORM.created_by | VARCHAR(64) NULL | 从JWT Token提取 |
| DeviceORM.created_at | DATETIME | 自动生成 |
| DeviceORM.updated_at | DATETIME | 自动更新 |
| DeviceORM.version | INTEGER DEFAULT 1 | 乐观锁版本号 |

### 10.2 规则 (RuleORM)

| DB 字段 | DB 类型 | 说明 |
|---------|--------|------|
| RuleORM.rule_id | VARCHAR(16) PK | 后端自动生成 |
| RuleORM.enabled | BOOLEAN DEFAULT TRUE | 通过enable/disable API切换 |
| RuleORM.created_by | VARCHAR(64) NULL | 从JWT Token提取 |
| RuleORM.created_at | DATETIME | 自动生成 |
| RuleORM.version | INTEGER DEFAULT 1 | 乐观锁版本号 |

### 10.3 告警 (AlarmORM)

| DB 字段 | DB 类型 | 说明 |
|---------|--------|------|
| AlarmORM.alarm_id | VARCHAR(16) PK | 后端自动生成 |
| AlarmORM.acknowledged_at | DATETIME NULL | 确认时自动填充 |
| AlarmORM.acknowledged_by | VARCHAR(64) NULL | 从JWT Token提取 |
| AlarmORM.recovered_at | DATETIME NULL | 恢复时自动填充 |
| AlarmORM.version | INTEGER DEFAULT 1 | 乐观锁版本号 |

### 10.4 用户 (UserORM)

| DB 字段 | DB 类型 | 说明 |
|---------|--------|------|
| UserORM.user_id | VARCHAR(16) PK | 后端自动生成 |
| UserORM.must_change_password | BOOLEAN DEFAULT FALSE | 首次登录标记 |
| UserORM.password_changed_at | DATETIME NULL | 密码修改时间 |
| UserORM.created_at | DATETIME | 自动生成 |
| UserORM.updated_at | DATETIME NULL | 自动更新 |
| UserORM.version | INTEGER DEFAULT 1 | 乐观锁版本号 |

### 10.5 告警静默 (AlarmSilenceORM)

| DB 字段 | DB 类型 | 说明 |
|---------|--------|------|
| AlarmSilenceORM.id | VARCHAR(36) PK | 后端自动生成UUID |
| AlarmSilenceORM.operator | VARCHAR(64) DEFAULT 'system' | 从JWT Token提取 |
| AlarmSilenceORM.created_at | DATETIME | 自动生成 |

### 10.6 审计日志 (AuditLogORM)

| DB 字段 | DB 类型 | 说明 |
|---------|--------|------|
| AuditLogORM.id | INTEGER PK AUTO | 自动递增 |
| AuditLogORM.audit_id | VARCHAR(32) UNIQUE | 后端自动生成 |
| AuditLogORM.timestamp | DATETIME | 自动生成 |
| AuditLogORM.action | VARCHAR(16) | 后端自动记录 |
| AuditLogORM.user_id | VARCHAR(64) | 从JWT Token提取 |
| AuditLogORM.resource_type | VARCHAR(32) | 后端自动记录 |
| AuditLogORM.resource_id | VARCHAR(64) | 后端自动记录 |
| AuditLogORM.resource_name | VARCHAR(128) | 后端自动记录 |
| AuditLogORM.old_value | TEXT DEFAULT '{}' | 后端自动记录 |
| AuditLogORM.new_value | TEXT DEFAULT '{}' | 后端自动记录 |
| AuditLogORM.changes | TEXT DEFAULT '{}' | 后端自动记录 |
| AuditLogORM.ip_address | VARCHAR(45) | 从请求提取 |
| AuditLogORM.user_agent | VARCHAR(256) | 从请求提取 |
| AuditLogORM.session_id | VARCHAR(64) | 从请求提取 |

### 10.7 安全相关表（无前端表单，纯后端使用）

| 表名 | 用途 |
|------|------|
| revoked_tokens | 已撤销JWT令牌 |
| login_attempts | IP级登录失败计数 |
| account_lockouts | 账户+IP级锁定 |
| global_login_failures | 全局登录失败记录 |
| global_account_lockouts | 全局账户锁定 |
| password_reset_attempts | IP级密码重置计数 |
| password_reset_user_rates | 用户级密码重置计数 |
| used_password_reset_tokens | 已使用重置令牌 |
| password_reset_ip_attempts | IP级重置使用计数 |
| cache_queue | 数据缓存队列 |

---

## 附：字段映射差异说明

| 差异类型 | 涉及页面 | 说明 |
|---------|---------|------|
| API无对应DB列 | AI模型 | `preprocess_config`, `postprocess_config`, `batch_size`, `max_concurrent`, `timeout_ms`, `device_preference` 仅存于Pydantic，未映射到ORM列 |
| 前端无对应表单 | 告警 | 告警由引擎自动生成，前端仅展示和操作(确认/恢复/抑制/静默) |
| JSON嵌套存储 | 设备/规则 | `config`, `points`, `conditions`, `notify_channels`, `trigger_value` 在DB中为TEXT(JSON)，在API层为结构化Pydantic模型 |
| 配置文件存储 | 通知/平台 | 通知配置和平台配置存储在YAML文件(`configs/config.yaml`)，不经过数据库 |
| 前端仅校验 | 登录 | `confirm_password` 仅前端校验一致性，不提交到API |
| 密码哈希 | 用户 | 前端/API为明文密码，DB存储bcrypt哈希(VARCHAR(128)) |
