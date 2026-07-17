const fs = require('fs')
const path = require('path')

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
    if (objMatch) { stack.push(objMatch[1]); continue }
    const kvMatch = trimmed.match(/^(\w+)\s*:\s*['"`]/)
    if (kvMatch) { stack.push(kvMatch[1]); keys.push(stack.join('.')); stack.pop(); continue }
    if (trimmed === '},' || trimmed === '}') { stack.pop(); continue }
  }
  return keys
}

const zhKeys = new Set(extractKeysFromTS('src/i18n/zh-CN.ts'))

// Check _tc() calls
const enumContent = fs.readFileSync('src/utils/enumLabels.ts', 'utf8')
const tcMatches = [...enumContent.matchAll(/_tc\(['"]([^'"]+)['"]\)/g)]
const missingTc = []
for (const m of tcMatches) {
  if (!zhKeys.has(m[1])) {
    missingTc.push(m[1])
  }
}

console.log('=== _tc() calls with MISSING i18n keys ===')
console.log('Total:', missingTc.length)
missingTc.forEach(k => console.log('  ' + k))

// Also do a comprehensive scan of ALL files for t() and _tc() calls
function walk(dir) {
  const results = []
  const items = fs.readdirSync(dir, { withFileTypes: true })
  for (const item of items) {
    const fullPath = path.join(dir, item.name)
    if (item.isDirectory() && item.name !== 'node_modules' && item.name !== 'i18n') {
      results.push(...walk(fullPath))
    } else if (item.isFile() && (item.name.endsWith('.vue') || item.name.endsWith('.ts'))) {
      results.push(fullPath)
    }
  }
  return results
}

const allFiles = walk('src')
const allMissing = new Map()

for (const file of allFiles) {
  const content = fs.readFileSync(file, 'utf8')
  const lines = content.split('\n')
  
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (line.trim().startsWith('//')) continue
    
    // Match t('key') - static only, skip dynamic (containing + or ${)
    const tMatches = [...line.matchAll(/\bt\(\s*['"]([^'"]+)['"]\s*[,)]/g)]
    for (const m of tMatches) {
      const key = m[1]
      if (key.includes('${')) continue
      if (!zhKeys.has(key)) {
        if (!allMissing.has(key)) allMissing.set(key, [])
        allMissing.get(key).push({ file, line: i + 1 })
      }
    }
    
    // Match _tc('key')
    const tcMatches = [...line.matchAll(/\b_tc\(\s*['"]([^'"]+)['"]\s*\)/g)]
    for (const m of tcMatches) {
      const key = m[1]
      if (!zhKeys.has(key)) {
        if (!allMissing.has(key)) allMissing.set(key, [])
        allMissing.get(key).push({ file, line: i + 1 })
      }
    }
  }
}

console.log('')
console.log('=== ALL static t()/_tc() calls with MISSING i18n keys ===')
console.log('Total unique missing keys:', allMissing.size)
for (const [key, locations] of allMissing) {
  console.log(`  "${key}"`)
  for (const loc of locations) {
    const relPath = path.relative('.', loc.file).replace(/\\/g, '/')
    console.log(`    -> ${relPath}:${loc.line}`)
  }
}
