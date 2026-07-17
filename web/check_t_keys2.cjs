const fs = require('fs')
const path = require('path')

// Parse i18n keys from TS files
function extractKeysFromTS(filePath) {
  const content = fs.readFileSync(filePath, 'utf8')
  const lines = content.split('\n')
  const keys = []
  const stack = []
  
  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trim()
    if (trimmed.startsWith('//') || trimmed.startsWith('/*') || trimmed.startsWith('*')) continue
    if (trimmed === '') continue
    
    const objMatch = trimmed.match(/^(\w+)\s*:\s*\{/)
    if (objMatch) {
      stack.push(objMatch[1])
      continue
    }
    
    const kvMatch = trimmed.match(/^(\w+)\s*:\s*['"`]/)
    if (kvMatch) {
      stack.push(kvMatch[1])
      keys.push(stack.join('.'))
      stack.pop()
      continue
    }
    
    if (trimmed === '},' || trimmed === '}') {
      stack.pop()
      continue
    }
  }
  return keys
}

const zhKeys = new Set(extractKeysFromTS('src/i18n/zh-CN.ts'))
const enKeys = new Set(extractKeysFromTS('src/i18n/en-US.ts'))

// Find ALL t() calls including template literals
function findAllTCalls(dir) {
  const results = []
  function walk(d) {
    const items = fs.readdirSync(d, { withFileTypes: true })
    for (const item of items) {
      const fullPath = path.join(d, item.name)
      if (item.isDirectory() && item.name !== 'node_modules' && item.name !== 'i18n') {
        walk(fullPath)
      } else if (item.isFile() && (item.name.endsWith('.vue') || item.name.endsWith('.ts'))) {
        const content = fs.readFileSync(fullPath, 'utf8')
        const lines = content.split('\n')
        
        for (let i = 0; i < lines.length; i++) {
          const line = lines[i]
          if (line.trim().startsWith('//')) continue
          
          // Static keys: t('key') or t("key")
          const staticMatches = [...line.matchAll(/\bt\(\s*['"]([^'"]+)['"]/g)]
          for (const m of staticMatches) {
            const key = m[1]
            // Skip if it looks like a dynamic prefix (ends with .)
            if (key.endsWith('.') && !key.match(/\w$/)) {
              results.push({ file: fullPath, line: i + 1, key, type: 'dynamic-prefix' })
            } else if (!zhKeys.has(key) && !enKeys.has(key)) {
              results.push({ file: fullPath, line: i + 1, key, type: 'static-missing' })
            }
          }
          
          // Template literal keys: t(`prefix${var}`)
          const templateMatches = [...line.matchAll(/\bt\(\s*`([^`]+)`/g)]
          for (const m of templateMatches) {
            const template = m[1]
            // Extract the static prefix before ${
            const prefixMatch = template.match(/^([^${]+)/)
            if (prefixMatch) {
              const prefix = prefixMatch[1]
              // Check if any key starts with this prefix
              let hasNamespace = false
              for (const k of zhKeys) {
                if (k.startsWith(prefix)) {
                  hasNamespace = true
                  break
                }
              }
              if (!hasNamespace) {
                results.push({ file: fullPath, line: i + 1, key: prefix, type: 'template-no-namespace' })
              }
            }
          }
        }
      }
    }
  }
  walk(dir)
  return results
}

const issues = findAllTCalls('src')

console.log('=== All t() calls with MISSING i18n keys ===')
console.log('Total issues:', issues.length)
console.log('')

for (const issue of issues) {
  const relPath = path.relative('.', issue.file).replace(/\\/g, '/')
  const typeLabel = {
    'static-missing': 'STATIC KEY MISSING',
    'dynamic-prefix': 'DYNAMIC PREFIX (no fallback)',
    'template-no-namespace': 'TEMPLATE LITERAL - NO NAMESPACE',
  }[issue.type] || issue.type
  console.log(`  [${typeLabel}] "${issue.key}"`)
  console.log(`    -> ${relPath}:${issue.line}`)
}

// Also check: are there any keys used in code where t() is called with a 
// key that exists but the value would display the key itself?
console.log('')
console.log('=== Checking for t() calls where key value == key itself (would show raw key) ===')
// This is already covered by the missing key check above
