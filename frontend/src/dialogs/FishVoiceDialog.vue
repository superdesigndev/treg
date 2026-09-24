<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div  class="scrim" role="dialog" aria-modal="true" aria-labelledby="fish-voice-dialog-title" @click.self="closeFishVoiceDialog">
      <div class="modal" style="width:min(470px,94vw);padding:18px 20px">
        <div class="hd"><b id="fish-voice-dialog-title">{{fishVoiceDialog.action==='rename'?'Rename voice':'Delete voice'}}</b><button class="btn sm ico" @click="closeFishVoiceDialog" aria-label="Close">✕</button></div>
        <template v-if="fishVoiceDialog.action!=='delete'">
          <div class="field" style="margin-top:14px"><label>Voice name</label><input :ref="el => setElement('fishVoiceName', el)" v-model="fishVoiceDialog.name" maxlength="100" @keyup.enter="submitFishVoiceDialog"/></div>
        </template>
        <p v-else style="margin:14px 0 0">Delete <b>{{fishVoiceDialog.voice.display_name||fishVoiceDialog.voice.upstream_id}}</b>? This cannot be undone.</p>
        <p v-if="fishVoiceDialog.error" class="err" style="margin:10px 0 0">{{fishVoiceDialog.error}}</p>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:18px"><button class="btn sm" :disabled="fishVoiceDialog.busy" @click="closeFishVoiceDialog">Cancel</button><button class="btn sm" :class="{primary:fishVoiceDialog.action!=='delete',danger:fishVoiceDialog.action==='delete'}" :disabled="fishVoiceDialog.busy || (fishVoiceDialog.action!=='delete'&&!fishVoiceDialog.name.trim())" @click="submitFishVoiceDialog">{{fishVoiceDialog.busy?'Working…':fishVoiceDialog.action==='rename'?'Save name':'Delete voice'}}</button></div>
      </div>
    </div>
</template>
