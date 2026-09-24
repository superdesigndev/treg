<script>
import { useDashboard } from '../state/context'
import BrandMark from '../components/BrandMark.vue'
export default { components: { BrandMark }, setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-modal="true" >
      <div class="modal" style="width:min(470px,94vw)">
        <div style="padding:26px 26px 22px">
          <div class="brand" style="color:var(--accent);font-size:15px;letter-spacing:.5px;margin-bottom:12px"><BrandMark/>treg</div>
          <h2 style="margin:0 0 8px;font-size:20px">You're invited 👋</h2>
          <p class="sub" style="margin:0 0 16px">{{pendingInvites.length===1 ? 'Accept to call the team\'s tools with' : 'You have '+pendingInvites.length+' pending invites — pick the teams to join and call their tools with'}} <b>no API keys on your machine</b>.</p>
          <label v-for="inv in sortedInvites" :key="inv.id" style="display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;margin-bottom:8px;cursor:pointer">
            <input type="checkbox" v-model="inviteSel[inv.id]" :disabled="inviteBusy" style="accent-color:var(--accent)"/>
            <span style="flex:1;min-width:0"><b>{{inv.name}}</b> <span class="role" :class="inv.role">{{inv.role}}</span><br/>
              <span class="sub" style="font-size:12px">invited by {{inv.invited_by||'a teammate'}}</span></span>
          </label>
          <div v-if="inviteErr" class="banner" style="margin:4px 0 8px">{{inviteErr}}</div>
          <button class="btn primary" style="width:100%;margin-top:2px" @click="acceptSelectedInvites" :disabled="inviteBusy || !selectedInvites.length">{{inviteBusy?'Joining…':(selectedInvites.length===1?'Accept & join '+selectedInvites[0].name+' →':'Accept & join '+selectedInvites.length+' teams →')}}</button>
          <button class="btn" style="width:100%;margin-top:10px" @click="declineInvite" :disabled="inviteBusy">{{inviteFirstRun ? 'Create my own team instead' : 'Not now'}}</button>
        </div>
      </div>
    </div>
</template>
