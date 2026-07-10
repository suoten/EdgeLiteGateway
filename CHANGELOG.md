# Changelog

All notable changes to EdgeLite Gateway are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-09

### Added
- Initial community release of EdgeLite Gateway
- 13 industrial protocol drivers: Modbus TCP/RTU, OPC UA (client + server), BACnet, IEC 60870-5-104, EtherNet/IP (Allen-Bradley), FINS (Omron), MC Protocol (Mitsubishi), ABB Robot, Siemens S7, DNP3, MQTT, SNMP
- ONNX Runtime AI inference engine with model hot-loading
- Vue 3 + Naive UI frontend with dashboard, device management, rule engine, and alarm system
- FastAPI backend with JWT authentication, RBAC (admin/operator/viewer), and audit logging
- SQLite + InfluxDB dual storage (hot config in SQLite, time-series in InfluxDB)
- Event bus with transactional outbox for zero-loss alarm delivery
- Configuration hot-reload with exponential backoff error handling
- Webhook and MQTT northbound data forwarding
- Comprehensive DevOps infrastructure:
  - CI pipeline (lint, type-check, test+coverage, build, Docker build)
  - CD pipeline (semantic versioning, GHCR image publishing, GitHub Releases)
  - Docker Compose stack (EdgeLite + InfluxDB + Mosquitto + Prometheus + Grafana)
  - Kubernetes manifests and Helm chart for production deployment
  - Pre-commit hooks for code quality enforcement
- Comprehensive test suite (943 tests, all passing)
- Prometheus metrics endpoint (/metrics) with Grafana dashboard provisioning
- Health check endpoints (/health/live, /health/ready) for Kubernetes probes
- Graceful shutdown with 30-second drain timeout
- Security hardening:
  - SignAndEncrypt default for OPC UA connections
  - Certificate validation with auto-renewal for self-signed certs
  - Container security (read-only filesystem, cap_drop ALL, no-new-privileges)
  - Secret management via environment variables with :? fail-fast validation

### Security
- OPC UA default security mode changed from None (plaintext) to SignAndEncrypt
- Certificate expiry validation prevents silent degradation to unencrypted connections
- OPC UA authentication failures now reject connections (return None) instead of granting Anonymous role
- TLS certificate loading properly awaited (was silently skipped)
- Video upload endpoint enforces 10MB size limit to prevent OOM
- Python sandbox uses module-level `__builtins__` namespace isolation (prevents global builtins pollution TOCTOU)
- Event outbox maintains consistency: persist failure prevents delivery (no orphan events)
- Modbus TOCTOU race condition fixed: client re-validated inside lock context after serial acquisition
- Modbus broadcast path correctly differentiated: slave_id=0 requires broadcast_enabled, slave_id 1-247 uses unicast

### Fixed
- IEC 60870-5-104: SBO (Select Before Operate) confirmation flow and TESTFR heartbeat retry reset
- BACnet: Service code for ConfirmedWriteProperty (14→15), bit string unused bits parsing, Error PDU context tag parsing, segment reassembly via _SegmentReassembler
- Omron FINS: UDP retransmission with exponential backoff (10ms→200ms cap), socket.timeout vs OSError differentiation
- Mitsubishi MC Protocol: binary vs ascii communication mode via setaccessopt, with graceful fallback
- Allen-Bradley EtherNet/IP: Watchdog failure counting with reconnect threshold, Large Forward Open auto-degradation
- Modbus: Shared constants imported from modbus_base (fixes `is` identity checks), broadcast logic corrected
- FastAPI pagination: removed alias="page_size" so query parameter name matches field name
- Rule engine: logic field validator converts to uppercase (accepts "and"/"or"/"not"), notify_channels allows empty list
- AI stats endpoint: hasattr guard for model_dump() on dict responses

### DevOps
- GitHub Actions CI workflow with 5 parallel jobs (lint, typecheck, test, build, docker)
- GitHub Actions CD workflow with semantic version tagging and GHCR publishing
- Docker multi-stage build (frontend-builder → python-builder → runtime)
- Docker Compose with health checks, resource limits, and security hardening for all services
- Prometheus + Grafana monitoring stack with alerting rules
- Kubernetes manifests (Namespace, ConfigMap, Secret, Deployment, Service)
- Helm chart with configurable values for production K8s deployment
- Pre-commit hooks (secret detection, Pydantic/ORM consistency, exception handling)
- ruff lint + format, pyright type checking, pytest-cov coverage (60% floor)
