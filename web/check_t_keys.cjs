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
    
    // Match object opening: word: {
    const objMatch = trimmed.match(/^(\w+)\s*:\s*\{/)
    if (objMatch) {
      stack.push(objMatch[1])
      continue
    }
    
    // Match key-value: word: 'value' or word: "value" or word: `value`
    const kvMatch = trimmed.match(/^(\w+)\s*:\s*['"`]/)
    if (kvMatch) {
      stack.push(kvMatch[1])
      keys.push(stack.join('.'))
      stack.pop()
      continue
    }
    
    // Match closing brace
    if (trimmed === '},' || trimmed === '}') {
      stack.pop()
      continue
    }
  }
  return keys
}

// Find all t() calls in code files
function findTSCalls(dir) {
  const results = []
  function walk(d) {
    const items = fs.readdirSync(d, { withFileTypes: true })
    for (const item of items) {
      const fullPath = path.join(d, item.name)
      if (item.isDirectory() && item.name !== 'node_modules' && item.name !== 'i18n') {
        walk(fullPath)
      } else if (item.isFile() && (item.name.endsWith('.vue') || item.name.endsWith('.ts'))) {
        const content = fs.readFileSync(fullPath, 'utf8')
        
        // Match t('key') or t("key") or t(`key`) - static keys only
        const staticMatches = content.matchAll(/\bt\(\s*['"`]([^'"`]+)['"`]/g)
        for (const match of staticMatches) {
          results.push({ file: fullPath, key: match[1], line: content.substring(0, match.index).split('\n').length })
        }
        
        // Match t('prefix' + variable) - dynamic keys
        const dynamicMatches = content.matchAll(/\bt\(\s*['"`]([^'"`]+)['"`]\s*\+/g)
        for (const match of dynamicMatches) {
          results.push({ file: fullPath, key: match[1] + '+DYNAMIC', line: content.substring(0, match.index).split('\n').length, dynamic: true })
        }
      }
    }
  }
  walk(dir)
  return results
}

const zhKeys = new Set(extractKeysFromTS('src/i18n/zh-CN.ts'))
const enKeys = new Set(extractKeysFromTS('src/i18n/en-US.ts'))

const tCalls = findTSCalls('src')

// Check for missing keys
const missingKeys = new Map() // key -> [{file, line}]
const dynamicPrefixes = new Map() // prefix -> [{file, line}]

for (const call of tCalls) {
  if (call.dynamic) {
    // For dynamic keys, check if the prefix exists as a namespace
    const prefix = call.key.replace('+DYNAMIC', '')
    // Check if any key starts with this prefix
    let hasNamespace = false
    for (const k of zhKeys) {
      if (k.startsWith(prefix)) {
        hasNamespace = true
        break
      }
    }
    if (!hasNamespace) {
      if (!dynamicPrefixes.has(prefix)) dynamicPrefixes.set(prefix, [])
      dynamicPrefixes.get(prefix).push({ file: call.file, line: call.line })
    }
    continue
  }
  
  // Skip keys with variables
  if (call.key.includes('${') || call.key.includes('${')) continue
  
  if (!zhKeys.has(call.key) && !enKeys.has(call.key)) {
    if (!missingKeys.has(call.key)) missingKeys.set(call.key, [])
    missingKeys.get(call.key).push({ file: call.file, line: call.line })
  }
}

console.log('=== Static t() keys MISSING from i18n files ===')
console.log('Total missing:', missingKeys.size)
console.log('')
for (const [key, locations] of missingKeys) {
  console.log(`  "${key}" (${locations.length} usage${locations.length > 1 ? 's' : ''})`)
  for (const loc of locations) {
    const relPath = path.relative('.', loc.file).replace(/\\/g, '/')
    console.log(`    -> ${relPath}:${loc.line}`)
  }
}

console.log('')
console.log('=== Dynamic t() key prefixes with NO matching namespace ===')
console.log('Total:', dynamicPrefixes.size)
console.log('')
for (const [prefix, locations] of dynamicPrefixes) {
  console.log(`  "${prefix}" (${locations.length} usage${locations.length > 1 ? 's' : ''})`)
  for (const loc of locations) {
    const relPath = path.relative('.', loc.file).replace(/\\/g, '/')
    console.log(`    -> ${relPath}:${loc.line}`)
  }
}

// Also check for keys that exist in zh-CN but NOT en-US (would show Chinese in English mode)
console.log('')
console.log('=== Keys in zh-CN but MISSING in en-US ===')
const onlyZh = [...zhKeys].filter(k => !enKeys.has(k))
console.log('Total:', onlyZh.length)
onlyZh.forEach(k => console.log('  ' + k))

console.log('')
console.log('=== Keys in en-US but MISSING in zh-CN ===')
const onlyEn = [...enKeys].filter(k => !zhKeys.has(k))
console.log('Total:', onlyEn.length)
onlyEn.forEach(k => console.log('  ' + k))
