// Which file and line a validator refusal points at. The hub names a field path ("steps[1].call",
// "inputs.domain", "check.fields"); the line is where the last named key appears, walking the path
// in order through the file's text. Good enough to put the error next to the right line.
export function fileOf(field: string): string {
  const head = field.split(/[.[]/)[0]
  if (head === 'script') return 'script'
  if (head === 'check') return 'check'
  if (head === 'readme') return 'readme'
  if (head === 'data') return 'data'
  return 'manifest'
}

export function lineOf(text: string, field: string): number | null {
  const parts = field.split('.').flatMap(p => p.replace(/\[\d+\]/g, '').split('.')).filter(Boolean)
  const keys = fileOf(field) === 'manifest' ? parts : parts.slice(1)
  let pos = 0, found = -1
  for (const k of keys) {
    const re = new RegExp(`"${k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"\\s*:`, 'g')
    re.lastIndex = pos
    const m = re.exec(text)
    if (!m) break
    found = m.index; pos = m.index + m[0].length
  }
  if (found < 0) return null
  return text.slice(0, found).split('\n').length
}
