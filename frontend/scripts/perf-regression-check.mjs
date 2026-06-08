import { readdirSync, readFileSync } from 'node:fs'

function assert(condition, message) {
  if (!condition) {
    throw new Error(message)
  }
}

const staticAssetsDir = new URL('../../static/assets/', import.meta.url)
const mainEntry = readFileSync(new URL('../src/main.tsx', import.meta.url), 'utf8')

const assetFiles = readdirSync(staticAssetsDir)
const jsFiles = assetFiles.filter((name) => name.endsWith('.js'))
const nonEntryJsFiles = jsFiles.filter((name) => !name.startsWith('index-'))

assert(
  !mainEntry.includes('StrictMode'),
  'main.tsx 仍然包含 StrictMode，开发环境会重复触发副作用。',
)

assert(
  nonEntryJsFiles.length > 0,
  `期望构建后存在额外懒加载 chunk，但当前只有入口脚本：${jsFiles.join(', ')}`,
)

console.log('性能回归检查通过：入口未启用 StrictMode，且存在懒加载 chunk。')
