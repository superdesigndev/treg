<script>
import { useDashboard } from '../state/context'
import ProviderLogo from './ProviderLogo.vue'

// One credential for a catalog provider, on Connections and on its provider's page: a connection
// (`a.c`) or a key saved as a secret named for the provider (`a.s`). The status says whether an agent
// can call it now; when it cannot, the one step that fixes it sits on the card itself.
export default {
  components: { ProviderLogo },
  props: {
    a: { type: Object, required: true },        // a connAccounts row
    manage: { type: Boolean, default: false },  // link to the provider's page (Connections only)
  },
  setup: useDashboard,
  data: () => ({ fixOpen: false }),  // the second-credential form, opened from its button
  computed: {
    owner() { return (this.a.c || this.a.s).owner },
    // Broken or about to be: renewing is the card's primary step rather than a link.
    fixable() { return this.a.st.key === 'reconnect' || this.a.st.key === 'failing' },
    renewLabel() { return this.a.pasted ? 'Replace key' : 'Reconnect' },
    caps() {
      const ask = this.a.p ? { provider: this.a.p, conn: this.a.c } : null
      return (this.a.c.capabilities || []).map(cap => this.capLabel(cap, ask)).join(', ')
    },
    missing() { return (this.a.c.missing_capabilities || []).map(cap => this.capLabel(cap, { provider: this.a.p, conn: this.a.c })).join(' + ') },
  },
}
</script>

<template>
<div class="pl-card cn-card" :class="'cn-'+a.st.tone">
  <div class="cn-top">
    <ProviderLogo :service="a.service" large />
    <span class="cn-id">
      <b>{{manage || a.s ? a.name : (a.c.resource_name || a.name)}}</b>
      <!-- The tool name is what an agent calls: several accounts of one provider each get their own.
           Only worth a line once it says more than the provider's own id. -->
      <span v-if="a.c && a.c.name!==a.service" class="pl-meta" :title="'treg call '+a.c.name">{{a.c.name}}</span>
    </span>
    <span class="cn-st" :title="a.st.title">{{a.st.label}}</span>
  </div>

  <p class="cn-what">
    <template v-if="a.s">{{authLabel(a.p)}}, saved as a secret<template v-if="a.st.tone==='quiet'">. The connected one is used instead</template></template>
    <template v-else-if="a.st.key==='paused'">{{a.c.paused_message}}</template>
    <template v-else>
      <template v-if="manage && (a.c.resource_name || a.c.resource_ref)">{{a.c.resource_name || a.c.resource_ref}} · </template>
      {{a.pasted ? authLabel(a.p) : (caps || 'Connected account')}}</template>
    <template v-if="owner"> · added by {{short(owner)}}</template>
  </p>

  <!-- A named key: remove it. Pasting one through the provider's button replaces it in use. -->
  <div v-if="a.s" class="cn-acts">
    <span class="cn-links">
      <a v-if="manage" :href="'/app/marketplace/'+encodeURIComponent(a.service)" @click.prevent="openProvider(a.service)">Manage</a>
      <button class="cn-del" :class="{armed:confirmDelSecret===a.s.id}" @click="deleteSecret(a.s)">
        {{confirmDelSecret===a.s.id ? 'Click again to remove' : 'Remove'}}</button>
    </span>
  </div>

  <!-- Paused by this deployment: the connection is kept as it is, so the only step is removing it. -->
  <div v-else-if="a.st.key==='paused'" class="cn-acts">
    <span class="cn-links">
      <button class="cn-del" :class="{armed:confirmDisc===a.c.id}" @click="disconnect(a.c)">
        {{confirmDisc===a.c.id ? 'Click again to remove' : 'Disconnect'}}</button>
    </span>
  </div>

  <div v-else-if="a.st.key==='second' && fixOpen" class="cn-fix">
    <p>{{a.c.extra_credential_note}}</p>
    <div class="cn-fix-r">
      <input class="bindinput" type="password" :placeholder="a.c.extra_credential_label||'Second credential'"
             :aria-label="a.c.extra_credential_label||'Second credential'"
             v-model="extraCred[a.c.id]" @keyup.enter="saveExtraCred(a.c)"/>
      <button class="pl-btn sm" :disabled="!extraCred[a.c.id] || extraBusy===a.c.id" @click="saveExtraCred(a.c)">
        {{extraBusy===a.c.id?'Saving…':'Save'}}</button>
      <button class="pl-btn sm ghost" @click="fixOpen=false">Cancel</button>
    </div>
  </div>

  <div v-else class="cn-acts">
    <button v-if="a.st.key==='second'" class="pl-btn sm" @click="fixOpen=true">Add {{(a.c.extra_credential_label||'second credential').toLowerCase()}}</button>
    <button v-if="fixable" class="pl-btn sm" :disabled="connBusy" @click="renewConnection(a)">{{renewLabel}}</button>
    <button v-else-if="a.st.key==='choose' || (!manage && a.c.supports_discovery)" class="pl-btn sm" :class="{ghost:a.st.key!=='choose'}"
            @click="openResources(a.c)">Choose {{a.c.resource_label||'account'}}</button>
    <button v-if="!manage && missing && a.p" class="pl-btn sm ghost" :disabled="connBusy"
            @click="startConnect(a.p, a.c)" :title="'Ask for '+a.c.missing_capabilities.join(', ')+' as well'">Add {{missing}}</button>
    <span class="cn-links">
      <a v-if="manage" :href="'/app/marketplace/'+encodeURIComponent(a.service)" @click.prevent="openProvider(a.service)">Manage</a>
      <template v-else>
        <button @click="renameConnection(a.c)" title="Change the tool name an agent calls for this account">Rename</button>
        <button v-if="!fixable && a.p" :disabled="connBusy" @click="renewConnection(a)">{{renewLabel}}</button>
      </template>
      <button class="cn-del" :class="{armed:confirmDisc===a.c.id}" @click="disconnect(a.c)">
        {{confirmDisc===a.c.id ? 'Click again to remove' : (a.pasted ? 'Remove' : 'Disconnect')}}</button>
    </span>
  </div>
</div>
</template>
