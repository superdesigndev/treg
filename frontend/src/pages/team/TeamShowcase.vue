<script>
import { useDashboard } from '../../state/context'
import Avatar from '../../components/ui/Avatar.vue'
import { IMAGE_RULES, imageProblem, uploadImage } from '../../images'
import { CATEGORIES, MAX_APPS, MAX_SHOTS, blankDraft, draftOf, moved, reviewedChanged, revisionOf, slotsUsed, statusOf, usageChanged, usageDraftOf, usageText } from './showcase'

// The team's entries on the public showcase (/showcase): apply, edit, withdraw. Owners and admins
// only (TeamPage shows the tab on canAdmin). Every name here starts with `sc` so it never shadows
// the dashboard state this component also reads (useDashboard).
const CONSENTS = [
  ['show_calls', 'Call tier', 'A rounded band of monthly calls, and a place on the Most active and Rising boards.'],
  ['show_streak', 'Streak', 'How many days in a row the app has made calls.'],
  ['show_providers', 'Providers', 'How many providers the app uses.'],
  ['show_categories', 'Categories', 'Its top capability categories.'],
]

export default {
  components: { Avatar },
  setup: useDashboard,
  data: () => ({
    CATEGORIES, CONSENTS, MAX_APPS, MAX_SHOTS, IMAGE_RULES,
    scApps: [], scLoaded: false, scClosed: false, scErr: '', scMsg: '',
    scForm: null, scFormErr: '', scStale: false, scSaving: false,
    scUpBusy: '', scUpErr: '', scUploadsOff: false, scConfirm: null, scRemoveConfirm: null,
    scKeys: [], scKeysErr: '',
  }),
  computed: {
    scUsed() { return slotsUsed(this.scApps) },
    scFull() { return this.scUsed >= MAX_APPS },
    scCanUpload() { return this.uploadsOn && !this.scUploadsOff },
  },
  watch: { activeOrgId() { this.scForm = null; this.scLoad() } },
  mounted() { this.scLoad() },
  methods: {
    scStatus: statusOf,
    scUsageText: usageText,
    async scLoadKeys() {
      this.scKeys = []; this.scKeysErr = ''
      try { this.scKeys = await this.api('/showcase/teams/' + this.activeOrgId + '/keys') }
      catch (e) { this.scKeysErr = e.detail || 'Could not load the API keys (' + e.status + ').' }
    },
    scKeyTaken(k) { return k.app_id != null && (!this.scForm || k.app_id !== this.scForm.id) },
    scKeyOwner(k) { const a = this.scApps.find(x => x.id === k.app_id); return a ? (a.pending || a.live || {}).name : 'another app' },
    async scLoad() {
      const id = this.activeOrgId
      if (!id) return
      this.scErr = ''
      try {
        const apps = await this.api('/showcase/teams/' + id + '/apps')
        if (id !== this.activeOrgId) return
        this.scApps = apps; this.scClosed = false
      } catch (e) {
        if (id !== this.activeOrgId) return
        this.scApps = []
        if (e.status === 404) this.scClosed = true
        else this.scErr = e.detail || 'Could not load the showcase (' + e.status + ').'
      } finally { this.scLoaded = true }
    },
    scNew() {
      this.scMsg = ''; this.scFormErr = ''; this.scUpErr = ''; this.scStale = false
      this.scForm = { id: null, version: 0, live: false, draft: blankDraft(), base: null, usage: usageDraftOf(null) }
      this.scLoadKeys()
    },
    scEdit(app) {
      this.scMsg = ''; this.scFormErr = ''; this.scUpErr = ''; this.scStale = false
      this.scForm = { id: app.id, version: app.version, live: !!app.approved_at, draft: draftOf(app), app,
                      base: JSON.stringify(revisionOf(draftOf(app))), usage: usageDraftOf(app) }
      this.scLoadKeys()
    },
    // After "this app changed": start again from what the server holds now.
    async scReloadForm() {
      const id = this.scForm && this.scForm.id
      await this.scLoad()
      const app = this.scApps.find(a => a.id === id)
      if (app) this.scEdit(app); else this.scForm = null
    },
    async scSave() {
      const f = this.scForm
      if (!f || this.scSaving) return
      this.scSaving = true; this.scFormErr = ''; this.scStale = false
      const fields = revisionOf(f.draft)
      const json = { 'content-type': 'application/json' }
      const usage = { source: f.usage.source, key_ids: f.usage.source === 'keys' ? f.usage.keyIds : [] }
      const usageNew = usageChanged(f.app || null, f.usage)
      if (usage.source === 'keys' && !usage.key_ids.length) {
        this.scFormErr = 'Pick at least one API key, or count the whole team.'; this.scSaving = false; return
      }
      try {
        // The fields are reviewed; where the activity comes from is applied at once.
        let id = f.id, sent = false
        if (f.id === null) {
          const created = await this.api('/showcase/teams/' + this.activeOrgId + '/apps', { method: 'POST', headers: json, body: JSON.stringify(fields) })
          // From here on the form edits the new entry, so a retry after a failed key step adds no second app.
          Object.assign(f, { id: created.id, version: created.version, app: created, base: JSON.stringify(fields) })
          id = created.id
          this.scMsg = 'Sent for review. Nothing is public until treg approves it.'
          sent = true
        } else if (JSON.stringify(fields) !== f.base) {
          const edited = await this.api('/showcase/apps/' + f.id, { method: 'PATCH', headers: json, body: JSON.stringify({ expected_version: f.version, fields }) })
          // Sent: a retry after a failed key step must not send the same change again.
          const reviewed = reviewedChanged(JSON.parse(f.base), fields)
          Object.assign(f, { version: edited.version, app: edited, base: JSON.stringify(fields) })
          this.scMsg = !reviewed ? 'Saved. The leaderboard shows it now.'
            : f.live ? 'Change sent for review. The live version stays until treg approves the change. What the leaderboard may show is saved now.'
            : 'Change sent for review.'
          sent = true
        }
        if (usageNew) {
          await this.api('/showcase/apps/' + id + '/usage', { method: 'PUT', headers: json, body: JSON.stringify(usage) })
          if (!sent) this.scMsg = 'Saved. The activity boards use it from the next hourly count.'
        } else if (!sent) {
          this.scFormErr = 'Nothing changed.'; return
        }
        this.scForm = null
        await this.scLoad()
      } catch (e) {
        this.scFormErr = e.detail || 'Could not save (' + e.status + ').'
        this.scStale = e.status === 409 && /changed/.test(this.scFormErr)
      } finally { this.scSaving = false }
    },
    async scWithdraw(app) {
      if (this.scConfirm !== app.id) { this.scConfirm = app.id; return }
      this.scConfirm = null; this.scErr = ''; this.scMsg = ''
      try {
        await this.api('/showcase/apps/' + app.id, { method: 'DELETE' })
        if (this.scForm && this.scForm.id === app.id) this.scForm = null
        await this.scLoad()
      } catch (e) { this.scErr = e.detail || 'Could not withdraw (' + e.status + ').' }
    },
    async scRemove(app) {
      if (this.scRemoveConfirm !== app.id) { this.scRemoveConfirm = app.id; return }
      this.scRemoveConfirm = null; this.scErr = ''; this.scMsg = ''
      try {
        await this.api('/showcase/apps/' + app.id + '/remove', { method: 'POST' })
        this.scMsg = 'Removed from the showcase. Ask treg if you want it shown again.'
        await this.scLoad()
      } catch (e) { this.scErr = e.detail || 'Could not remove (' + e.status + ').' }
    },
    scClose() { if (!this.scSaving) { this.scForm = null; this.scFormErr = '' } },
    scPick(ref) { const input = this.$refs[ref]; if (input) input.click() },
    async scUpload(kind, event) {
      const input = event.target, file = input.files && input.files[0]
      input.value = ''
      if (!file || !this.scForm) return
      this.scUpErr = imageProblem(kind, file)
      if (this.scUpErr) return
      this.scUpBusy = kind
      try {
        const staged = await uploadImage(kind, file, this.headers())
        const pic = { key: staged.key, url: staged.preview_url }
        const d = this.scForm.draft
        if (kind === 'showcase_icon') d.icon = pic
        else if (d.shots.length < MAX_SHOTS && !d.shots.some(s => s.key === pic.key)) d.shots = [...d.shots, pic]
      } catch (e) {
        if (e.status === 503) this.scUploadsOff = true
        else this.scUpErr = e.detail || 'The image could not be uploaded.'
      } finally { this.scUpBusy = '' }
    },
    scMove(i, by) { this.scForm.draft.shots = moved(this.scForm.draft.shots, i, by) },
    scDropShot(i) { this.scForm.draft.shots = this.scForm.draft.shots.filter((_, j) => j !== i) },
  },
}
</script>

<template>
<section class="sc" aria-labelledby="sc-title">
  <div class="sc-head">
    <div>
      <h2 id="sc-title" class="lbl" style="margin:0">Showcase</h2>
      <p class="sub" style="margin:4px 0 0;font-size:12px;max-width:640px">
        List an app your team built with treg on the public showcase. Nothing is public until treg approves it.
        An edit to a live app is reviewed too; the live version stays until the change is approved.
      </p>
    </div>
    <div v-if="!scClosed" class="sc-slots">
      <span class="sub" style="margin:0">{{scUsed}} of {{MAX_APPS}} used</span>
      <button class="btn primary sm" :disabled="scFull || !scLoaded" @click="scNew">＋ Apply with an app</button>
    </div>
  </div>

  <p v-if="scClosed" class="sub">The showcase is not open on this registry.</p>
  <template v-else>
    <p v-if="scFull" class="tut-notice" role="status" style="margin:10px 0 0">Your team uses all {{MAX_APPS}} places. Withdraw or remove an entry to apply with another. Rejected entries do not count.</p>
    <div v-if="scErr" class="banner" role="alert" style="margin-top:10px">{{scErr}}</div>
    <div v-if="scMsg" class="tut-notice" role="status" style="margin-top:10px;display:flex;justify-content:space-between;gap:12px">
      <span>{{scMsg}}</span><button class="btn sm ico" aria-label="Dismiss" @click="scMsg=''">✕</button>
    </div>

    <p v-if="!scLoaded" class="muted" style="margin-top:12px">Loading…</p>
    <p v-else-if="!scApps.length && !scForm" class="sub" style="margin-top:12px">Your team has no apps on the showcase yet.</p>
    <ul v-else class="sc-list" aria-label="Your showcase entries">
      <li v-for="a in scApps" :key="a.id" class="card sc-app">
        <Avatar :src="(a.pending||a.live||{}).icon_url" :name="(a.pending||a.live||{}).name||'?'" :size="40" />
        <div class="sc-app-main">
          <div class="sc-app-name"><b>{{(a.pending||a.live||{}).name}}</b>
            <span class="chip sc-chip" :class="scStatus(a).tone">{{scStatus(a).text}}</span></div>
          <div class="sub" style="margin:2px 0 0;font-size:12px">{{(a.pending||a.live||{}).tagline}}</div>
          <div class="sub sc-usage" style="margin:2px 0 0;font-size:12px">{{scUsageText(a.usage)}}</div>
          <div v-if="a.status!=='rejected' && a.approved_at && a.reject_reason && !a.pending" class="sub" style="margin:4px 0 0;font-size:12px">Your last change was not approved: {{a.reject_reason}}</div>
        </div>
        <div class="row-actions sc-app-actions">
          <button class="btn sm" @click="scEdit(a)">Edit</button>
          <button v-if="!a.approved_at || a.pending" class="btn sm" :class="{danger:scConfirm===a.id}" @click="scWithdraw(a)">
            {{scConfirm===a.id ? 'Confirm withdraw' : (a.approved_at ? 'Withdraw change' : 'Withdraw')}}</button>
          <button v-if="a.status==='approved'" class="btn sm" :class="{danger:scRemoveConfirm===a.id}" @click="scRemove(a)">
            {{scRemoveConfirm===a.id ? 'Confirm remove' : 'Remove from showcase'}}</button>
        </div>
      </li>
    </ul>

    <div v-if="scForm" class="scrim" role="dialog" aria-modal="true" aria-labelledby="sc-form-title" v-dialog="scClose" @click.self="scClose">
      <div class="modal sc-modal">
        <div class="hd"><b id="sc-form-title">{{scForm.id===null ? 'Apply with an app' : 'Edit ' + (scForm.draft.name || 'entry')}}</b>
          <button type="button" class="btn sm" aria-label="Close" @click="scClose">✕</button></div>
        <form class="sc-form" :aria-label="scForm.id===null?'Apply with an app':'Edit showcase entry'" @submit.prevent="scSave">
          <div class="sc-body">
            <p class="explain">{{scForm.live
              ? 'This app is live. Your change is reviewed before it replaces the live version.'
              : 'Nothing is public until treg approves it. You can edit it while it waits.'}}</p>

            <section class="sc-section" aria-labelledby="sc-about">
              <h3 id="sc-about" class="lbl">About the app</h3>
              <div class="sc-grid">
                <label class="sc-field"><span>App name</span><input v-model="scForm.draft.name" maxlength="60" required placeholder="Lead scout"></label>
                <label class="sc-field"><span>Link (https)</span><input v-model="scForm.draft.url" type="url" placeholder="https://" required></label>
                <label class="sc-field sc-wide"><span>Tagline</span><input v-model="scForm.draft.tagline" maxlength="80" required placeholder="What it does, in one line"></label>
                <label class="sc-field sc-wide"><span>Description <i>optional, up to 500 characters</i></span>
                  <textarea v-model="scForm.draft.description" maxlength="500" rows="3"></textarea></label>
                <label class="sc-field"><span>Category</span>
                  <select v-model="scForm.draft.category" required>
                    <option value="" disabled>Pick a category</option>
                    <option v-for="[v,l] in CATEGORIES" :key="v" :value="v">{{l}}</option>
                  </select></label>
              </div>
            </section>

            <section class="sc-section" aria-labelledby="sc-maker">
              <h3 id="sc-maker" class="lbl">Maker <i>optional</i></h3>
              <div class="sc-grid">
                <label class="sc-field"><span>Maker name</span><input v-model="scForm.draft.maker_name" maxlength="60" placeholder="Your name"></label>
                <label class="sc-field"><span>X handle</span><input v-model="scForm.draft.x_handle" maxlength="16" placeholder="@handle"></label>
                <label class="sc-field sc-wide"><span>Launch post</span><input v-model="scForm.draft.launch_post_url" type="url" placeholder="https://x.com/…"></label>
              </div>
            </section>

            <section class="sc-section" aria-labelledby="sc-pictures">
              <h3 id="sc-pictures" class="lbl">Images</h3>
              <p v-if="!scCanUpload" class="sub sc-off" role="status">Image uploads are not enabled on this registry. You can apply without images.</p>
              <template v-else>
                <div class="sc-iconrow">
                  <div class="sc-iconbox">
                    <img v-if="scForm.draft.icon" class="sc-icon" :src="scForm.draft.icon.url" alt="App icon">
                    <span v-else class="sc-icon sc-icon-empty" aria-hidden="true">{{(scForm.draft.name||'?').charAt(0).toUpperCase()}}</span>
                  </div>
                  <div>
                    <b class="sc-pic-title">Icon</b>
                    <p class="sub sc-hint">{{IMAGE_RULES.showcase_icon.hint}}</p>
                    <input ref="scIconInput" type="file" accept="image/png,image/jpeg,image/webp" style="display:none" @change="scUpload('showcase_icon',$event)">
                    <div class="sc-btns">
                      <button type="button" class="btn sm" :disabled="!!scUpBusy" @click="scPick('scIconInput')">{{scUpBusy==='showcase_icon'?'Uploading…':(scForm.draft.icon?'Replace icon':'Upload icon')}}</button>
                      <button v-if="scForm.draft.icon" type="button" class="btn sm" @click="scForm.draft.icon=null">Remove icon</button>
                    </div>
                  </div>
                </div>
                <div class="sc-shot-head"><b class="sc-pic-title">Screenshots</b>
                  <span class="sub sc-hint">Up to {{MAX_SHOTS}}; the first is the cover. {{IMAGE_RULES.showcase_shot.hint}}</span></div>
                <ol class="sc-shots" aria-label="Screenshots">
                  <li v-for="(s,i) in scForm.draft.shots" :key="s.key" class="sc-shot">
                    <img :src="s.url" :alt="'Screenshot '+(i+1)">
                    <span v-if="i===0" class="chip sc-cover">Cover</span>
                    <span class="sc-shot-tools">
                      <button type="button" class="btn sm ico" :disabled="i===0" :aria-label="'Move screenshot '+(i+1)+' up'" @click="scMove(i,-1)">←</button>
                      <button type="button" class="btn sm ico" :disabled="i===scForm.draft.shots.length-1" :aria-label="'Move screenshot '+(i+1)+' down'" @click="scMove(i,1)">→</button>
                      <button type="button" class="btn sm ico" :aria-label="'Remove screenshot '+(i+1)" @click="scDropShot(i)">✕</button>
                    </span>
                  </li>
                  <li v-if="scForm.draft.shots.length<MAX_SHOTS" class="sc-shot sc-shot-add">
                    <input ref="scShotInput" type="file" accept="image/png,image/jpeg,image/webp" style="display:none" @change="scUpload('showcase_shot',$event)">
                    <button type="button" class="btn sm" :disabled="!!scUpBusy" @click="scPick('scShotInput')">{{scUpBusy==='showcase_shot'?'Uploading…':'Add screenshot'}}</button>
                  </li>
                </ol>
                <p v-if="scUpErr" class="banner" role="alert" style="margin:8px 0 0">{{scUpErr}}</p>
              </template>
            </section>

            <section class="sc-section" aria-labelledby="sc-usage-title">
              <h3 id="sc-usage-title" class="lbl">Where its activity comes from</h3>
              <p class="sub sc-hint" style="margin:0 0 8px">The activity boards count these calls. Saved at once, without review. A rotated key keeps counting through its replacement.</p>
              <div class="sc-consent" role="radiogroup" aria-label="Activity source">
                <label class="sc-check"><input v-model="scForm.usage.source" type="radio" value="team">
                  <span><b>The whole team</b><span class="sub">Every call your team makes.</span></span></label>
                <label class="sc-check"><input v-model="scForm.usage.source" type="radio" value="keys">
                  <span><b>Only some API keys</b><span class="sub">For a team with more than one app.</span></span></label>
              </div>
              <div v-if="scForm.usage.source==='keys'" class="sc-keys">
                <p v-if="scKeysErr" class="banner" role="alert">{{scKeysErr}}</p>
                <p v-else-if="!scKeys.length" class="sub sc-hint">Your team has no API keys yet.</p>
                <label v-for="k in scKeys" :key="k.id" class="sc-key" :class="{off:scKeyTaken(k)}">
                  <input v-model="scForm.usage.keyIds" type="checkbox" :value="k.id" :disabled="scKeyTaken(k)" :aria-label="'API key ' + k.name">
                  <span><b>{{k.name}}</b> <code v-if="k.prefix">{{k.prefix}}…</code>
                    <span class="sub">{{scKeyTaken(k) ? 'counts for ' + scKeyOwner(k) : (k.last_used_at ? 'last used ' + k.last_used_at.slice(0,10) : 'not used yet')}}</span></span>
                </label>
              </div>
            </section>

            <section class="sc-section" aria-labelledby="sc-consent-title">
              <h3 id="sc-consent-title" class="lbl">What the showcase may show</h3>
              <p class="sub sc-hint" style="margin:0 0 8px">Upvotes always show. Each item below is saved at once, without review. Only rounded figures are shown, never exact counts, spend or your team's name.</p>
              <div class="sc-consent">
                <label v-for="[k,title,text] in CONSENTS" :key="k" class="sc-check">
                  <input v-model="scForm.draft[k]" type="checkbox">
                  <span><b>{{title}}</b><span class="sub">{{text}}</span></span>
                </label>
              </div>
            </section>

            <div v-if="scFormErr" class="banner" role="alert">{{scFormErr}}
              <button v-if="scStale" type="button" class="btn sm" style="margin-left:8px" @click="scReloadForm">Reload this app</button></div>
          </div>
          <div class="sc-foot">
            <button type="button" class="btn" @click="scClose">Cancel</button>
            <button type="submit" class="btn primary" :disabled="scSaving || !!scUpBusy">{{scSaving ? 'Sending…' : 'Send for review'}}</button>
          </div>
        </form>
      </div>
    </div>
  </template>
</section>
</template>

<style>
.sc-head { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; flex-wrap:wrap; }
.sc-slots { display:flex; gap:12px; align-items:center; }
.sc-list { list-style:none; padding:0; margin:14px 0 0; display:grid; gap:10px; }
.sc-app { display:flex; gap:12px; align-items:center; padding:12px 14px; }
.sc-app-main { flex:1; min-width:0; }
.sc-app-name { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.sc-app-actions { display:flex; gap:6px; flex-wrap:wrap; }
.sc-chip.ok { color:var(--green); border-color:color-mix(in srgb,var(--green) 30%,transparent); background:none; }
.sc-chip.review { color:var(--amber); border-color:color-mix(in srgb,var(--amber) 30%,transparent); background:none; }
.sc-chip.bad { color:var(--red); border-color:color-mix(in srgb,var(--red) 30%,transparent); background:none; white-space:normal; }
.sc-chip.off { color:var(--muted); border-color:var(--line2); background:none; }
.sc-modal { width:min(720px,95vw); max-height:90vh; display:flex; flex-direction:column; overflow:hidden; }
.sc-form { display:flex; flex-direction:column; min-height:0; flex:1; }
.sc-body { padding:16px 20px 8px; overflow:auto; }
.sc-section { padding:14px 0; border-top:1px solid var(--line); }
.sc-section:first-of-type { border-top:0; padding-top:4px; }
.sc-section > .lbl { margin:0 0 10px; }
.sc-section > .lbl i, .sc-field span i { font-style:normal; text-transform:none; letter-spacing:0; color:var(--muted2); font-weight:400; margin-left:4px; }
.sc-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px 14px; }
.sc-field { display:flex; flex-direction:column; gap:6px; min-width:0; font-size:12.5px; color:var(--ink); font-weight:500; }
.sc-field input, .sc-field textarea, .sc-field select {
  width:100%; box-sizing:border-box; background:var(--surface); border:1px solid var(--line2); color:var(--ink);
  border-radius:10px; padding:9px 11px; font:400 13.5px var(--sans); }
.sc-field input, .sc-field select { height:40px; }
.sc-field textarea { resize:vertical; min-height:76px; line-height:1.45; }
.sc-field input:focus, .sc-field textarea:focus, .sc-field select:focus { outline:none; border-color:var(--ink); }
.sc-field input::placeholder, .sc-field textarea::placeholder { color:var(--muted2); }
.sc-wide { grid-column:1 / -1; }
.sc-hint { font-size:12px; margin:2px 0 8px; }
.sc-pic-title { font-size:13px; }
.sc-btns { display:flex; gap:8px; flex-wrap:wrap; }
.sc-iconrow { display:flex; gap:14px; align-items:flex-start; margin-bottom:14px; }
.sc-icon { display:grid; place-items:center; width:72px; height:72px; border-radius:16px; object-fit:cover; border:1px solid var(--line); flex:none; }
.sc-icon-empty { background:var(--panel2,var(--bg)); color:var(--muted); font:600 26px var(--sans); }
.sc-shot-head { display:flex; flex-direction:column; gap:2px; }
.sc-shots { list-style:none; padding:0; margin:0; display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:10px; }
.sc-shot { position:relative; border:1px solid var(--line); border-radius:10px; overflow:hidden; aspect-ratio:16/10; background:var(--panel2,var(--bg)); }
.sc-shot img { width:100%; height:100%; object-fit:cover; display:block; }
.sc-shot-add { display:grid; place-items:center; border-style:dashed; }
.sc-cover { position:absolute; left:6px; top:6px; background:var(--surface); }
.sc-shot-tools { position:absolute; right:6px; bottom:6px; display:flex; gap:4px; }
.sc-shot-tools .btn { background:var(--surface); min-width:26px; padding:2px 6px; }
.sc-consent { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }
.sc-check { display:flex; gap:10px; align-items:flex-start; padding:10px 12px; border:1px solid var(--line); border-radius:10px; cursor:pointer; font-size:13px; }
.sc-check:has(input:checked) { border-color:var(--ink); }
.sc-check input { margin-top:2px; }
.sc-check .sub { display:block; margin:2px 0 0; font-size:12px; }
.sc-off { font-size:12px; margin:0; }
.sc-keys { display:flex; flex-direction:column; gap:6px; margin-top:10px; }
.sc-key { display:flex; gap:10px; align-items:center; padding:8px 12px; border:1px solid var(--line); border-radius:10px; font-size:13px; cursor:pointer; }
.sc-key.off { opacity:.55; cursor:default; }
.sc-key code { font:12px var(--mono, monospace); color:var(--muted); }
.sc-key .sub { margin-left:6px; font-size:12px; }
.sc-foot { display:flex; justify-content:flex-end; gap:10px; padding:12px 20px; border-top:1px solid var(--line); background:var(--surface); }
@media (max-width:700px) { .sc-grid, .sc-consent { grid-template-columns:1fr; } .sc-app { flex-wrap:wrap; } .sc-body { padding:14px 14px 6px; } }
</style>
