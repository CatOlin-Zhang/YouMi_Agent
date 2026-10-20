// 临时探针：检查 DSH session log（zstd 多帧 + jsonl）的事件结构，确认可提取的指标字段。
// zstd 文件是 append-only 的，一行日志可能对应一个独立帧，必须用流式解码而不是 zstdDecompressSync。
import fs from 'node:fs'
import { createZstdDecompress } from 'node:zlib'

const path = process.argv[2]
const decoder = createZstdDecompress()
const chunks = []
decoder.on('data', chunk => chunks.push(chunk))
await new Promise((resolve, reject) => {
  decoder.on('end', resolve)
  decoder.on('error', reject)
  fs.createReadStream(path).pipe(decoder)
})

const raw = Buffer.concat(chunks).toString('utf8')
const lines = raw.trim().split('\n').filter(line => line.length > 0)

const types = {}
const samples = {}
for (const line of lines) {
  let event
  try { event = JSON.parse(line) } catch { continue }
  types[event.type] = (types[event.type] || 0) + 1
  if (!samples[event.type]) samples[event.type] = line
}

console.log('== lines:', lines.length)
console.log('== types:', JSON.stringify(types, null, 2))
console.log('\n== one sample per type ==')
for (const [type, line] of Object.entries(samples)) {
  console.log(`\n--- ${type} ---\n${line.slice(0, 600)}`)
}
