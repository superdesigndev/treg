// Follow the conversation the way chat apps do: while the reader is at the bottom, stay there as
// text streams and cards land (a ResizeObserver, not a guess about how much was added); once they
// scroll up, stop following and offer a way back; at the bottom again, follow again. `active` says
// whether there is a conversation to follow (an empty page reads from the top).
import { onUnmounted, ref, watch, type Ref } from 'vue'

const NEAR = 48   // px from the bottom that still counts as "at the bottom"

export function useStickToBottom(scroller: Ref<HTMLElement | null>, content: Ref<HTMLElement | null>,
  active: () => boolean = () => true) {
  const pinned = ref(true)
  let observer: ResizeObserver | null = null
  let last = 0

  function atBottom(el: HTMLElement) { return el.scrollHeight - el.scrollTop - el.clientHeight < NEAR }

  function toBottom(smooth = false) {
    const el = scroller.value
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  }

  // Only the reader moving UP lets go; reaching the bottom (by any means) takes hold again. Our own
  // scrolls only ever move down, so they can never unpin, smooth or not.
  function onScroll() {
    const el = scroller.value
    if (!el) return
    if (atBottom(el)) pinned.value = true
    else if (el.scrollTop < last - 2) pinned.value = false
    last = el.scrollTop
  }

  // Jump down now and follow from here (sending a message, opening a conversation, the button).
  function follow(smooth = false) { pinned.value = true; toBottom(smooth) }

  watch([scroller, content], ([el, inner], [oldEl]) => {
    oldEl?.removeEventListener('scroll', onScroll)
    observer?.disconnect()
    if (!el || !inner) return
    el.addEventListener('scroll', onScroll, { passive: true })
    observer = new ResizeObserver(() => { if (pinned.value && active()) toBottom() })
    observer.observe(inner)
  }, { immediate: true, flush: 'post' })

  onUnmounted(() => { scroller.value?.removeEventListener('scroll', onScroll); observer?.disconnect() })
  return { pinned, follow }
}
