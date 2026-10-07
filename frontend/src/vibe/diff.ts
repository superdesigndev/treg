// What changed between two versions of the files, per file, as lines with a little context.
import { diffLines } from 'diff'

export type Line = { kind: 'add' | 'del' | 'ctx' | 'gap', text: string }
export type FileDiff = { file: string, added: number, removed: number, lines: Line[] }

export const FILE_LABELS: Record<string, string> = {
  manifest: 'recipe.json', script: 'run.js', check: 'check.json', readme: 'README.md', data: 'data.csv',
}

export function fileText(files: Record<string, unknown> | undefined, key: string): string {
  const v = files?.[key]
  if (v === undefined || v === null) return ''
  return typeof v === 'string' ? v : JSON.stringify(v, null, 2)
}

const CONTEXT = 3

export function diffFiles(before: Record<string, unknown> | undefined, after: Record<string, unknown> | undefined): FileDiff[] {
  const out: FileDiff[] = []
  for (const key of Object.keys(FILE_LABELS)) {
    const a = fileText(before, key), b = fileText(after, key)
    if (a === b) continue
    const lines: Line[] = []
    let added = 0, removed = 0
    for (const part of diffLines(a, b)) {
      const rows = part.value.replace(/\n$/, '').split('\n')
      if (part.added) { added += rows.length; rows.forEach(text => lines.push({ kind: 'add', text })) }
      else if (part.removed) { removed += rows.length; rows.forEach(text => lines.push({ kind: 'del', text })) }
      else rows.forEach(text => lines.push({ kind: 'ctx', text }))
    }
    out.push({ file: FILE_LABELS[key], added, removed, lines: trimContext(lines) })
  }
  return out
}

// Keep CONTEXT unchanged lines around each change; a run of more becomes one gap line.
function trimContext(lines: Line[]): Line[] {
  const near = lines.map(() => false)
  lines.forEach((l, i) => {
    if (l.kind === 'ctx') return
    for (let j = Math.max(0, i - CONTEXT); j <= Math.min(lines.length - 1, i + CONTEXT); j++) near[j] = true
  })
  const out: Line[] = []
  let skipped = 0
  lines.forEach((l, i) => {
    if (near[i]) {
      if (skipped) { out.push({ kind: 'gap', text: `${skipped} unchanged line${skipped === 1 ? '' : 's'}` }); skipped = 0 }
      out.push(l)
    } else skipped++
  })
  if (skipped) out.push({ kind: 'gap', text: `${skipped} unchanged line${skipped === 1 ? '' : 's'}` })
  return out
}
