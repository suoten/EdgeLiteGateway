# Security Policy

## Supported Versions

EdgeLite Gateway 社区版仅对**最新发布版本**提供安全修复。请始终使用最新 release，旧版本不再维护安全补丁。

| Version | Supported          |
|---------|--------------------|
| 1.0.x   | ✅ 最新发布版本     |
| < 1.0   | ❌ 不再维护         |

如需长期支持（LTS）或安全补丁回溯，请联系企业版支持：<suoten@163.com>。

## Reporting a Vulnerability

EdgeLite Gateway 重视社区安全披露。如果您发现安全漏洞或潜在风险，**请勿在公开 Issue 中提交**，请按以下流程私下报告：

### 报告渠道

- **邮箱**：<suoten@163.com>
- **主题**：`[SECURITY] EdgeLite 漏洞报告 - <简短描述>`
- **加密通信**：如需 PGP 加密，请先邮件索取公钥

### 报告内容

为加快评估与修复，请在报告中包含：

1. **漏洞描述**：清晰说明问题本质与潜在影响
2. **受影响版本**：明确受影响的 release 版本号或 commit hash
3. **复现步骤**：最小化复现步骤（PoC 优先），包括：
   - 操作系统 / Python 版本 / Node.js 版本
   - 相关配置（脱敏后）
   - 触发路径（API 端点 / 命令 / 配置项）
4. **影响评估**：可能的攻击场景、所需权限、是否需要认证
5. **建议修复方案**（可选）

### 响应时间承诺

| 阶段 | 承诺时间 |
|------|---------|
| 收到报告确认 | 2 个工作日内 |
| 初步评估与分级 | 7 个工作日内 |
| 修复版本发布 | 严重漏洞 30 天内 / 一般漏洞下个 release |
| 公开披露 | 修复发布后 90 天，或与报告者协商一致的时间 |

严重漏洞（CVSS ≥ 7.0）修复期间，我们会与报告者保持沟通，必要时提供临时缓解措施。

## Public Disclosure Policy

- 在修复版本发布前，**请勿公开披露漏洞细节**，避免被恶意利用
- 修复发布后，我们会在 CHANGELOG.md 中标注安全修复条目（CVE 编号如适用）
- 报告者将在修复公告中获得致谢（除非要求匿名）
- 我们支持负责任的披露，不会对善意安全研究采取法律行动

## Security Configuration Checklist

部署 EdgeLite Gateway 到生产环境前，请逐项确认：

- [ ] 修改 admin 默认密码（首次登录强制改密）
- [ ] 配置 `EDGELITE_SECURITY__SECRET_KEY`（`python -c "import secrets; print(secrets.token_urlsafe(32))"`）
- [ ] 配置 `EDGELITE_MASTER_KEY` 与 `EDGELITE_KDF_SALT`（生产环境必填，缺失将拒绝启动）
- [ ] 配置 InfluxDB 强 Token 与管理员密码
- [ ] 限制 CORS 源为实际前端域名（禁用 `["*"]`）
- [ ] 启用 HTTPS（Nginx + SSL 证书），HSTS 已配置
- [ ] 启用 nginx 限流（`limit_req_zone` / `limit_req` / `limit_conn`）
- [ ] 禁用 API 文档端点（`/docs` / `/redoc` / `/openapi.json` 已在生产配置中返回 404）
- [ ] 限制服务器仅开放 80/443 端口
- [ ] 配置日志级别为 WARNING 或 ERROR
- [ ] 启用 SQLite WAL 模式与 `busy_timeout=5000`（代码已默认）
- [ ] 配置定期备份策略（SQLite + InfluxDB）

## Security Features

EdgeLite Gateway 内置以下安全机制（详见 [README.md](README.md)）：

- JWT AccessToken(30min) + RefreshToken(7天)，支持 Token 撤销
- RBAC 三角色（admin / operator / viewer）× 22 权限项
- bcrypt(rounds=14, OWASP 2023) 密码哈希
- 登录失败 5 次锁定 15 分钟，支持 IP 与账号双维度限流
- CSRF Token 校验（X-CSRF-Token）
- 敏感字段（密码 / Token / API Key / 手机号等）正则脱敏
- Fernet 对称加密敏感凭据
- 审计日志全操作留痕
- 双向 TLS（mTLS）支持
- 固件 RSA-2048/4096 + ECDSA(P-256/P-384) 签名

## Contact

- 安全报告：<suoten@163.com>
- 一般 Issue：[GitHub Issues](https://github.com/suoten/EdgeLiteGateway/issues)
- 企业版安全咨询（国密 / 等保 / LDAP / SSO）：<suoten@163.com>
