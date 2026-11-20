<script>
import motion from './dropdownMotion.js'

// The dashboard's dropdown: a button that opens a listbox under it. Looks like the rest of the app
// in both themes, which the browser's own <select> popup never does, and keeps its keyboard rules:
// Enter, Space or ↓ opens it; ↑/↓ move; Enter picks; Escape or a click outside closes it. Past
// SEARCH_AT options it gets a search box, and typing filters the list.
let uid = 0
const SEARCH_AT = 8

// Optional leading icons, drawn on a 16px grid like the rest of the dashboard's line icons.
const ICONS = {
  clock: '<circle cx="8" cy="8" r="5.5"/><path d="M8 5v3.2l2 1.3"/>',
  key: '<circle cx="5.5" cy="10.5" r="2.5"/><path d="M7.3 8.7 13 3M10.5 5.5l1.5 1.5M12 4l1.5 1.5"/>',
  plug: '<circle cx="4" cy="8" r="2"/><circle cx="12" cy="4" r="2"/><circle cx="12" cy="12" r="2"/><path d="M5.8 7.1 10.2 5M5.8 8.9l4.4 2.1"/>',
  layers: '<path d="M8 2.5 14 5.5 8 8.5 2 5.5z"/><path d="M2 8.5l6 3 6-3"/><path d="M2 11l6 3 6-3"/>',
}

export default {
  mixins: [motion],
  props: {
    modelValue: { type: [String, Number], default: '' },
    // [{ value, label, hint? }]
    options: { type: Array, required: true },
    label: { type: String, required: true },   // the accessible name, e.g. "Group by"
    size: { type: String, default: 'md' },      // md (filters) | lg (page-level, like .pl-select)
    icon: { type: String, default: '' },        // one of ICONS
    disabled: { type: Boolean, default: false },
  },
  emits: ['update:modelValue', 'change'],
  data: () => ({ open: false, active: 0, query: '', id: 'sm-' + (++uid) }),
  computed: {
    current() { return this.options.find(o => o.value === this.modelValue) || this.options[0] },
    searchable() { return this.options.length > SEARCH_AT },
    shown() {
      const q = this.query.trim().toLowerCase()
      return q ? this.options.filter(o => String(o.label).toLowerCase().includes(q)) : this.options
    },
    iconPath() { return ICONS[this.icon] || '' },
  },
  watch: { query() { this.active = 0 } },
  beforeUnmount() { document.removeEventListener('pointerdown', this.outside, true) },
  methods: {
    toggle() { this.open ? this.close() : this.show() },
    show() {
      if (this.disabled) return
      this.open = true
      this.query = ''
      this.active = Math.max(this.shown.findIndex(o => o.value === this.modelValue), 0)
      this.motionOpen()
      document.addEventListener('pointerdown', this.outside, true)
      this.$nextTick(() => (this.searchable ? this.$refs.search : this.$refs.list)?.focus())
    },
    close(refocus = true) {
      if (!this.open) return
      this.open = false
      this.motionClose()
      document.removeEventListener('pointerdown', this.outside, true)
      if (refocus) this.$nextTick(() => this.$refs.button?.focus())
    },
    outside(e) { if (!this.$el.contains(e.target)) this.close(false) },
    pick(option) {
      if (!option) return
      this.close()
      if (option.value === this.modelValue) return
      this.$emit('update:modelValue', option.value)
      this.$emit('change', option.value)
    },
    onButtonKey(e) { if (['ArrowDown', 'ArrowUp'].includes(e.key)) { e.preventDefault(); this.show() } },
    onListKey(e) {
      const n = this.shown.length
      if (e.key === 'ArrowDown') { e.preventDefault(); if (n) this.active = (this.active + 1) % n }
      else if (e.key === 'ArrowUp') { e.preventDefault(); if (n) this.active = (this.active - 1 + n) % n }
      else if (e.key === 'Home' && !this.searchable) { e.preventDefault(); this.active = 0 }
      else if (e.key === 'End' && !this.searchable) { e.preventDefault(); this.active = n - 1 }
      else if (e.key === 'Enter' || (e.key === ' ' && !this.searchable)) { e.preventDefault(); this.pick(this.shown[this.active]) }
      else if (e.key === 'Escape' || e.key === 'Tab') { e.preventDefault(); this.close() }
      this.$nextTick(() => this.$refs.list?.querySelector('.active')?.scrollIntoView({ block: 'nearest' }))
    },
  },
}
</script>

<template>
  <div class="sm" :class="['sm-' + size, { open }]">
    <button ref="button" type="button" class="sm-button" :disabled="disabled" aria-haspopup="listbox"
            :aria-expanded="open" :aria-controls="id" :aria-label="label + ': ' + (current?.label || '')"
            :title="current?.label" @click="toggle" @keydown="onButtonKey">
      <svg v-if="iconPath" class="sm-icon" viewBox="0 0 16 16" aria-hidden="true" v-html="iconPath" />
      <span class="sm-value">{{current?.label}}</span>
      <svg class="sm-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" /></svg>
    </button>
    <div v-if="mounted" class="sm-pop t-dropdown" :class="phase" data-origin="top-left" @keydown="onListKey">
      <div v-if="searchable" class="sm-search">
        <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.5" /><path d="M10.5 10.5 14 14" /></svg>
        <input ref="search" v-model="query" type="text" :placeholder="'Search ' + label.toLowerCase()" :aria-label="'Search ' + label.toLowerCase()"
               role="combobox" :aria-controls="id" :aria-expanded="open" :aria-activedescendant="shown.length ? id + '-' + active : null" autocomplete="off" />
      </div>
      <ul :id="id" ref="list" class="sm-list" role="listbox" :tabindex="searchable ? -1 : 0" :aria-label="label"
          :aria-activedescendant="!searchable && shown.length ? id + '-' + active : null">
        <li v-for="(o, i) in shown" :id="id + '-' + i" :key="String(o.value)" role="option"
            :aria-selected="o.value === modelValue" :class="{ active: i === active }" :title="o.label"
            @mouseenter="active = i" @click="pick(o)">
          <span class="sm-label">{{o.label}}</span><small v-if="o.hint" class="muted">{{o.hint}}</small>
          <svg v-if="o.value === modelValue" class="sm-check" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" /></svg>
        </li>
        <li v-if="!shown.length" class="sm-none muted" role="presentation">No matches</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.sm{position:relative;display:inline-block;max-width:100%}
.sm-button{display:flex;align-items:center;gap:8px;width:100%;min-width:120px;max-width:260px;height:40px;padding:0 12px 0 14px;
  border:1px solid var(--line);border-radius:10px;background:var(--surface,var(--panel));color:var(--ink);font:13.5px var(--sans);cursor:pointer;
  transition:border-color .12s,box-shadow .12s}
.sm-lg .sm-button{height:52px;max-width:340px;padding:0 16px 0 18px;border:0;border-radius:14px;box-shadow:var(--shadow-sm);font-size:14px}
.sm-button:hover{border-color:color-mix(in srgb,var(--ink) 25%,var(--line))}
.sm.open .sm-button,.sm-button:focus-visible{outline:none;border-color:var(--ink);box-shadow:0 0 0 1px var(--ink)}
.sm-lg.open .sm-button,.sm-lg .sm-button:focus-visible{box-shadow:var(--shadow-sm),0 0 0 1.5px var(--ink)}
.sm-button:disabled{opacity:.55;cursor:default}
.sm-value{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:left}
.sm-icon{flex:none;width:15px;height:15px;fill:none;stroke:var(--muted);stroke-width:1.4;stroke-linecap:round;stroke-linejoin:round}
.sm-chevron{flex:none;width:14px;height:14px;fill:none;stroke:var(--muted);stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;transition:transform var(--dropdown-open-dur,250ms) var(--dropdown-ease,ease)}
.sm.open .sm-chevron{transform:rotate(180deg)}
.sm-pop{position:absolute;z-index:30;top:calc(100% + 6px);left:0;min-width:100%;width:max-content;max-width:min(360px,calc(100vw - 32px));
  background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow-md,0 8px 24px rgba(0,0,0,.14));overflow:hidden}
.sm-search{display:flex;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid var(--line)}
.sm-search svg{flex:none;width:14px;height:14px;fill:none;stroke:var(--muted);stroke-width:1.5;stroke-linecap:round}
/* The app's global input box (border, height, focus ring) would draw a box inside the menu. */
.sm-pop .sm-search input{flex:1;min-width:0;height:auto;margin:0;border:0;border-radius:0;background:none;box-shadow:none;color:var(--ink);font:13.5px var(--sans);outline:none;padding:4px 0}
.sm-list{max-height:300px;overflow:auto;margin:0;padding:5px;list-style:none;outline:none}
.sm-list li{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;color:var(--ink);font-size:13.5px;white-space:nowrap;cursor:pointer}
.sm-list li.active{background:var(--panel2)}
.sm-label{min-width:0;overflow:hidden;text-overflow:ellipsis}
.sm-list li small{margin-left:auto;font-size:11.5px}
.sm-check{flex:none;width:14px;height:14px;margin-left:auto;fill:none;stroke:var(--ink);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.sm-list li small + .sm-check{margin-left:0}
.sm-list li.sm-none{cursor:default}
</style>
