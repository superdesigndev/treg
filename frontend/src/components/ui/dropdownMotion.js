// The open/close orchestration for a `.t-dropdown` surface (transitions.dev menu dropdown): it
// mounts at its pre-open scale, gains `.is-open` on the next frame, and on close plays
// `.is-closing` for --dropdown-close-dur before it unmounts, so the next open starts from rest.
export const closeMs = () => parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dropdown-close-dur')) || 150

// Mixin state: `mounted` keeps the surface in the DOM through its close; `phase` is its class.
export default {
  data: () => ({ mounted: false, phase: '' }),
  methods: {
    motionOpen() {
      clearTimeout(this._closeTimer)
      this.mounted = true
      this.phase = ''
      requestAnimationFrame(() => requestAnimationFrame(() => { if (this.mounted) this.phase = 'is-open' }))
    },
    motionClose() {
      if (!this.mounted) return
      this.phase = 'is-closing'
      clearTimeout(this._closeTimer)
      this._closeTimer = setTimeout(() => { this.mounted = false; this.phase = '' }, closeMs())
    },
  },
  beforeUnmount() { clearTimeout(this._closeTimer) },
}
