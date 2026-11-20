import type { Directive } from 'vue'

/**
 * `v-dialog` on the element that carries role="dialog": one keyboard contract for every modal and
 * drawer instead of per-dialog code. While it is mounted, the dialog
 * - takes focus (its first form field, else the dialog itself),
 * - keeps Tab and Shift+Tab inside itself,
 * - closes on Escape when given a close function (a required decision passes none),
 * - and, once unmounted, hands focus back to the control that opened it.
 * Open dialogs form a stack, so only the topmost one answers the keyboard.
 */
type Close = (() => unknown) | null | undefined
interface Entry { el: HTMLElement, close: Close, opener: Element | null, fallback: Element | null }

const stack: Entry[] = []
const FOCUSABLE = 'a[href],area[href],button:not([disabled]),input:not([disabled]):not([type="hidden"]),'
  + 'select:not([disabled]),textarea:not([disabled]),summary,[contenteditable="true"],[tabindex]:not([tabindex="-1"])'
const FIELD = 'input:not([disabled]):not([type="hidden"]):not([type="checkbox"]):not([type="radio"]),select:not([disabled]),textarea:not([disabled])'

// checkVisibility also sees the content of a closed <details>, which keeps its boxes but takes no focus.
function visible(el: Element) {
  return typeof el.checkVisibility === 'function' ? el.checkVisibility({ visibilityProperty: true }) : el.getClientRects().length > 0
}
function focusables(root: HTMLElement) { return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(visible) }

function onKeydown(event: KeyboardEvent) {
  const top = stack[stack.length - 1]
  if (!top) return
  if (event.key === 'Escape') {
    if (!top.close) return
    // Capture phase: the page's own Escape handling (menus) must not also run for this press.
    event.preventDefault(); event.stopImmediatePropagation()
    top.close()
    return
  }
  if (event.key !== 'Tab') return
  const items = focusables(top.el)
  const active = document.activeElement
  if (!items.length) { event.preventDefault(); top.el.focus(); return }
  const first = items[0]!, last = items[items.length - 1]!
  if (!active || !top.el.contains(active) || active === top.el) {
    event.preventDefault(); (event.shiftKey ? last : first).focus()
  } else if (event.shiftKey && active === first) {
    event.preventDefault(); last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault(); first.focus()
  }
}

const entries = new WeakMap<HTMLElement, Entry>()

export const vDialog: Directive<HTMLElement, Close> = {
  mounted(el, binding) {
    const active = document.activeElement
    const owner = stack.find(entry => active && entry.el.contains(active))
    const entry: Entry = { el, close: binding.value, opener: active, fallback: owner ? owner.opener : null }
    entries.set(el, entry)
    stack.push(entry)
    if (stack.length === 1) document.addEventListener('keydown', onKeydown, true)
    if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '-1')
    // A dialog that already placed focus inside itself (an explicit ref focus) keeps it.
    if (active && el.contains(active)) return
    const field = [...el.querySelectorAll<HTMLElement>(FIELD)].find(visible)
    ;(field || el).focus({ preventScroll: !field })
  },
  updated(el, binding) {
    const entry = entries.get(el)
    if (entry) entry.close = binding.value
  },
  unmounted(el) {
    const entry = entries.get(el)
    if (!entry) return
    entries.delete(el)
    stack.splice(stack.indexOf(entry), 1)
    if (!stack.length) document.removeEventListener('keydown', onKeydown, true)
    // Focus moved on to something still on the page (another dialog, a page control): leave it.
    const active = document.activeElement
    if (active && active !== document.body && active.isConnected && !el.contains(active)) return
    const target = [entry.opener, entry.fallback].find(item => item instanceof HTMLElement && item.isConnected && item !== document.body)
    if (target) (target as HTMLElement).focus({ preventScroll: true })
  },
}

declare module 'vue' {
  interface GlobalDirectives { vDialog: typeof vDialog }
}
