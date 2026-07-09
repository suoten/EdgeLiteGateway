# i18n Full Audit Script Design

## Problem

EdgeLiteGateway has significant i18n gaps across backend and frontend:

1. Backend API error codes (`error_codes.py`) not mapped in frontend `ERROR_CODE_MAP`
2. Backend driver error codes (`i18n.py` TranslationStrings) have zero frontend mapping
3. Frontend Vue files contain hardcoded Chinese/English strings instead of `t()` calls
4. Backend API files return hardcoded Chinese in HTTPException detail / JSONResponse message
5. zh-CN and en-US translation files have asymmetric keys

Users see raw error codes like `ERR_SVC_LIST_FAILED` or untranslated Chinese text, making the system unusable in English mode.

## Solution

Create `scripts/audit_i18n_full.py` — a unified audit script that scans all 5 dimensions and produces a comprehensive report.

## Architecture

```
audit_i18n_full.py
├── Dimension 1: Backend API error codes → Frontend mapping gaps
├── Dimension 2: Backend driver error codes (i18n.py) → Frontend mapping gaps
├── Dimension 3: Frontend Vue hardcoded Chinese/English strings
├── Dimension 4: Backend API hardcoded Chinese strings
├── Dimension 5: zh-CN / en-US key asymmetry
├── Summary report (terminal + JSON)
└── Exit code: 1 if issues found, 0 if clean
```

## Dimension Details

### Dimension 1: Backend API Error Codes → Frontend Mapping

**Source files:**
- `src/edgelite/api/error_codes.py` — all `ERR_XXX` string constants
- `web/src/utils/errorCodes.ts` — `ERROR_CODE_MAP` keys
- `web/src/i18n/zh-CN.ts` / `en-US.ts` — `errorCodes` section keys

**Checks:**
- Backend error code not in `ERROR_CODE_MAP` → **mapping missing**
- `ERROR_CODE_MAP` entry points to `errorCodes.ERR_XXX` but that key doesn't exist in i18n files → **translation missing**
- `ERROR_CODE_MAP` entry points to a non-errorCodes i18n key (e.g. `login.rateLimited`) but that key doesn't exist → **invalid i18n key reference**

### Dimension 2: Backend Driver Error Codes → Frontend Mapping

**Source files:**
- `src/edgelite/services/i18n.py` — `TranslationStrings` class fields starting with `ERR_`
- `web/src/utils/errorCodes.ts` — `ERROR_CODE_MAP` keys
- `web/src/i18n/zh-CN.ts` / `en-US.ts` — any section

**Checks:**
- Driver error code field name (e.g. `ERR_MODBUS_CONN_FAILED`) not in `ERROR_CODE_MAP` → **unmapped**
- These are currently 100% unmapped; the audit will enumerate them all

### Dimension 3: Frontend Vue Hardcoded Strings

**Source files:**
- `web/src/views/**/*.vue`
- `web/src/components/**/*.vue`

**Detection rules:**
- Template: Chinese characters in text content or attribute values (not inside `t()` calls)
- Script: String literals with Chinese in `title:`, `label:`, `message:`, `placeholder:`, `description:` patterns
- Also detect hardcoded English in similar patterns (lower priority)
- Exclude: `t()` call arguments, comments (`//`, `/* */`, `<!-- -->`), `console.log`

**Whitelist:** Configurable via `--whitelist` YAML file to suppress known false positives.

### Dimension 4: Backend API Hardcoded Chinese

**Source files:**
- `src/edgelite/api/**/*.py`

**Detection rules:**
- `detail="中文"` or `detail='中文'` (HTTPException)
- `message="中文"` or `message='中文'` (JSONResponse)
- Dict string values with Chinese: `{"message": "中文"}`
- Exclude: Comments, logger statements, docstrings

### Dimension 5: zh-CN / en-US Asymmetry

**Source files:**
- `web/src/i18n/zh-CN.ts`
- `web/src/i18n/en-US.ts`

**Logic:** Reuse leaf key extraction from existing `check_i18n_symmetry.py`. Report keys only in one file.

## Output Format

### Terminal (colored)

```
============================================================
EdgeLiteGateway i18n Full Audit Report
============================================================

[1] Backend API Error Codes → Frontend Mapping
  Total backend error codes: 210
  Mapped in ERROR_CODE_MAP: 198
  Missing mapping: 12
  Missing i18n translation: 5
  Invalid i18n key reference: 3

  --- Missing in ERROR_CODE_MAP ---
    ERR_PLATFORM_DASHBOARD_FAILED  (PlatformErrors)
    ...

  --- Missing i18n translation ---
    ERR_SVC_LIST_FAILED → errorCodes.ERR_SVC_LIST_FAILED (key not found)
    ...

[2] Backend Driver Error Codes → Frontend Mapping
  Total driver error codes (i18n.py): 450
  Mapped in frontend: 0
  Unmapped: 450

[3] Frontend Vue Hardcoded Strings
  Files with hardcoded Chinese: 25
  Total hardcoded strings: 87

  DeviceList.vue:15  "设备列表"
  ...

[4] Backend API Hardcoded Chinese
  Files with hardcoded Chinese: 12
  Total hardcoded strings: 34

  devices.py:45  detail="设备不存在"
  ...

[5] zh-CN / en-US Asymmetry
  zh-CN only: 26 keys
  en-US only: 9 keys

============================================================
SUMMARY: 588 issues found
Exit code: 1
============================================================
```

### JSON (`audit_i18n_report.json`)

```json
{
  "timestamp": "2026-06-11T...",
  "dimensions": {
    "backend_api_error_codes": {
      "total": 210,
      "mapped": 198,
      "missing_map": [...],
      "missing_translation": [...],
      "invalid_i18n_ref": [...]
    },
    "backend_driver_error_codes": {
      "total": 450,
      "mapped": 0,
      "unmapped": [...]
    },
    "frontend_vue_hardcoded": {
      "files_affected": 25,
      "total_strings": 87,
      "details": [{"file": "...", "line": 15, "text": "..."}]
    },
    "backend_api_hardcoded": {
      "files_affected": 12,
      "total_strings": 34,
      "details": [{"file": "...", "line": 45, "context": "detail=..."}]
    },
    "i18n_asymmetry": {
      "zh_only": [...],
      "en_only": [...]
    }
  },
  "summary": {"total_issues": 588}
}
```

## CLI Interface

```bash
python scripts/audit_i18n_full.py              # Basic run
python scripts/audit_i18n_full.py --verbose     # Verbose output with file locations
python scripts/audit_i18n_full.py --json        # JSON only output
python scripts/audit_i18n_full.py --whitelist whitelist.yaml  # Load whitelist
```

## Whitelist Format (YAML)

```yaml
backend_api_missing_map:
  - ERR_PLATFORM_DASHBOARD_FAILED  # Internal use, not user-facing

vue_hardcoded_chinese:
  - "DeviceList.vue:.*品牌.*"  # Regex match

backend_api_hardcoded:
  - "health.py:.*健康状态.*"  # Health endpoint, not user-facing
```

## Implementation Notes

- Pure Python, no external dependencies beyond stdlib
- Reuse leaf key extraction logic from existing `check_i18n_symmetry.py`
- Reuse `t()` call detection from existing `check_i18n_coverage.py`
- Chinese character detection: `[\u4e00-\u9fff]` regex range
- All paths relative to project root for portability
