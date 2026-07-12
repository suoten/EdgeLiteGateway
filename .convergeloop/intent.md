# EdgeLite Gateway - 修复业务意图

## 项目概述
EdgeLite Gateway 是面向工业物联网场景的开源边缘 AI 网关（v1.0-Community 社区版）。
技术栈：FastAPI 0.110+ / Python 3.11+ 后端 + Vue3.4+ / Naive UI 前端 + ONNX Runtime 边缘推理 + SQLite/时序数据库 + Docker 部署。

核心能力：
- 13 种工业协议采集（Modbus/S7/OPC UA/MQTT/GB28181/JT808 等）
- 3 个自学习 AI 模型（传感器异常检测、视觉确认、自学习进化）
- 传感器 AI + 视觉 AI 双确认告警闭环
- 规则引擎联动、事件总线、审计日志
- RBAC 权限体系（管理员/操作员/查看者）
- 边缘侧推理，延迟 < 100ms，512MB 即可运行

## 修复目标
将社区版修复到**完美版（perfect / 生产可直部署）**状态：成熟度 ≥ 95，且项目达到可直接部署使用状态，不会测出问题。

## 强制要求（用户明确指定）
1. **全量修复**：必须强制修复所有发现的问题，包括历史遗留问题，不得跳过、不得提前退出。
2. **无法修复的明示**：对于确实无法修复的问题（如第三方库限制、架构根本性冲突），必须明确提示出来，给出规避方案并记录到 visible_unresolved 妥协项，绝不可静默吞掉。
3. **不留技术债**：不得用注释掉代码、try/except 吞异常、return None 等方式掩盖问题。
4. **遵循项目硬约束**：参见 project_memory.md 中的 Hard Constraints 与 Engineering Conventions（权限校验、表单验证、WAL 模式、asyncio.Lock 保护计数器、事件总线阻塞 put、调度器 gate 锁内验证等）。
5. **前后端协同评估**：必须检查前后端 API 端口/字段/契约一致性等协同问题。
6. **通用识别**：不局限于特定模块，需覆盖物联网协议驱动（如协议驱动不全）、JT/T 808 解编码、GB28181 平台 SIP/ZML 等各类细节问题。
7. **反复验证**：通过各种提示语让 AI 反复验证、反复修复发现的各种细节问题，直至完美。

## 重点关注领域
security / performance / architecture / testing / documentation / devops / code_quality / bug_fix（全部）
