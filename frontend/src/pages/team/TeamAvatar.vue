<script>
import { useDashboard } from '../../state/context'
import AvatarEdit from '../../components/ui/AvatarEdit.vue'
import { IMAGE_RULES, imageProblem, uploadImage } from '../../images'

// The active team's avatar (Team settings, owners and admins). Upload stages the file, then
// PUT /orgs/{id}/avatar makes it the team's; no key clears it back to initials.
export default {
  components: { AvatarEdit },
  setup: useDashboard,
  data: () => ({ taBusy: false, taErr: '', taOff: false, taHint: IMAGE_RULES.team_avatar.hint }),
  methods: {
    async taSet(key) {
      const org = this.activeOrg
      const r = await this.api('/orgs/' + this.activeOrgId + '/avatar', {
        method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ key }) })
      const row = this.myOrgs.find(o => o.org_id === org.org_id)
      if (row) row.avatar_url = r.avatar_url
    },
    async taUpload(event) {
      const input = event.target, file = input.files && input.files[0]
      input.value = ''
      if (!file) return
      this.taErr = imageProblem('team_avatar', file)
      if (this.taErr) return
      this.taBusy = true
      try { await this.taSet((await uploadImage('team_avatar', file, this.headers())).key) }
      catch (e) { if (e.status === 503) this.taOff = true; else this.taErr = e.detail || 'The avatar could not be saved.' }
      finally { this.taBusy = false }
    },
    async taRemove() {
      this.taBusy = true; this.taErr = ''
      try { await this.taSet(null) }
      catch (e) { this.taErr = e.detail || 'The avatar could not be removed.' }
      finally { this.taBusy = false }
    },
  },
}
</script>

<template>
<div style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
  <span class="lbl" style="margin:0">Team avatar</span>
  <p v-if="!uploadsOn || taOff" class="sub" style="margin:4px 0 0;font-size:12px">Avatar uploads are not enabled on this registry. The team shows its initials.</p>
  <template v-else>
    <p class="sub" style="margin:4px 0 12px;font-size:12px">Shown in the team picker and on this page. {{taHint}}</p>
    <div style="display:flex;gap:16px;align-items:center">
      <AvatarEdit :src="activeOrg && activeOrg.avatar_url" :name="activeName" :size="56" :busy="taBusy" label="Team avatar" @file="taUpload" />
      <button v-if="activeOrg && activeOrg.avatar_url" class="btn sm" :disabled="taBusy" @click="taRemove">Remove avatar</button>
    </div>
    <p v-if="taErr" class="banner" role="alert" style="margin:10px 0 0">{{taErr}}</p>
  </template>
</div>
</template>
