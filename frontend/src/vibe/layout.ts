// The three panels' layout: the conversations (left) and the files (right) open or closed and how
// wide, the chat taking the rest. Widths are dragged within limits, remembered per browser, and
// the side panels toggle from the header or the keyboard (Cmd/Ctrl+B, Cmd/Ctrl+\).
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

export const LIMITS = {
  left: { min: 200, max: 360, def: 250 },
  right: { min: 320, maxShare: 0.6, def: 460 },
  chatMin: 420,
  narrow: 900,          // below this the panels become tabs
}
const KEY = 'treg-vibe-layout'

type Saved = { left: number, right: number, leftOpen: boolean, rightOpen: boolean }

function load(): Saved {
  const d: Saved = { left: LIMITS.left.def, right: LIMITS.right.def, leftOpen: true, rightOpen: true }
  try { return { ...d, ...JSON.parse(localStorage.getItem(KEY) || '{}') } } catch { return d }
}

export function useLayout() {
  const s = reactive(load())
  const width = ref(window.innerWidth)
  const narrow = computed(() => width.value < LIMITS.narrow)
  const tab = ref<'chat' | 'files' | 'list'>('chat')
  const dragging = ref<'' | 'left' | 'right'>('')

  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(s)) } catch { /* convenience */ } }
  const clampLeft = (w: number) => Math.round(Math.max(LIMITS.left.min, Math.min(LIMITS.left.max, w)))
  const clampRight = (w: number) => {
    const room = width.value - (s.leftOpen ? s.left : 0) - LIMITS.chatMin
    const max = Math.max(LIMITS.right.min, Math.min(width.value * LIMITS.right.maxShare, room))
    return Math.round(Math.max(LIMITS.right.min, Math.min(max, w)))
  }
  const left = computed(() => clampLeft(s.left))
  const right = computed(() => clampRight(s.right))
  const columns = computed(() => narrow.value ? '1fr'
    : `${s.leftOpen ? left.value + 'px' : '0px'} minmax(${LIMITS.chatMin}px,1fr) ${s.rightOpen ? right.value + 'px' : '0px'}`)

  function toggle(which: 'left' | 'right') {
    if (narrow.value) { tab.value = tab.value === (which === 'left' ? 'list' : 'files') ? 'chat' : which === 'left' ? 'list' : 'files'; return }
    if (which === 'left') s.leftOpen = !s.leftOpen; else s.rightOpen = !s.rightOpen
    save()
  }

  function startDrag(which: 'left' | 'right', e: PointerEvent) {
    e.preventDefault()
    const x0 = e.clientX, w0 = which === 'left' ? left.value : right.value
    dragging.value = which
    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - x0
      if (which === 'left') s.left = clampLeft(w0 + dx); else s.right = clampRight(w0 - dx)
    }
    const up = () => { dragging.value = ''; window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); save() }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  // Arrow keys on a focused handle move it; Home puts the default back (double-click does too).
  function keyDrag(which: 'left' | 'right', e: KeyboardEvent) {
    const step = e.shiftKey ? 40 : 10
    const sign = which === 'left' ? 1 : -1
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault()
      const d = (e.key === 'ArrowRight' ? step : -step) * sign
      if (which === 'left') s.left = clampLeft(left.value + d); else s.right = clampRight(right.value + d)
      save()
    } else if (e.key === 'Home') reset(which)
  }
  function reset(which: 'left' | 'right') {
    if (which === 'left') s.left = LIMITS.left.def; else s.right = LIMITS.right.def
    save()
  }

  const onResize = () => { width.value = window.innerWidth }
  const onKey = (e: KeyboardEvent) => {
    if (!(e.metaKey || e.ctrlKey) || e.altKey) return
    if (e.key === 'b' || e.key === 'B') { e.preventDefault(); toggle('left') }
    else if (e.key === '\\') { e.preventDefault(); toggle('right') }
  }
  onMounted(() => { window.addEventListener('resize', onResize); window.addEventListener('keydown', onKey) })
  onUnmounted(() => { window.removeEventListener('resize', onResize); window.removeEventListener('keydown', onKey) })

  return { s, left, right, columns, narrow, tab, dragging, toggle, startDrag, keyDrag, reset }
}
