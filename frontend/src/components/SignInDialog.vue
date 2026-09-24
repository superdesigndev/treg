<script>
import { useDashboard } from '../state/context'
import BrandMark from './BrandMark.vue'
export default { components: { BrandMark }, setup: useDashboard }
</script>

<template>
<div class="lc-scrim" :class="{open:demo.signin}" @click.self="demo.signin=false">
    <div class="lc-modal" role="dialog" aria-modal="true" aria-label="Sign in">
      <button class="cls" @click="demo.signin=false" aria-label="Close">✕</button>
      <div style="font-size:22px;line-height:1"><BrandMark/></div>
      <h2 style="margin:8px 0 2px;font-family:var(--mono)">{{oauthSignin?'Sign in to continue connecting Treg':(invitePrefill?'Accept your invite':(shareGate?'Sign in to view it':(publicCatalog?'Start calling':'Make it yours')))}}</h2>
      <p v-if="oauthSignin" class="sub">After sign-in, review the requested access before you approve it.</p>
      <p v-else-if="invitePrefill" class="sub">Sign in with <b>{{invitePrefill}}</b> and you'll drop straight into the team.</p>
      <p v-else-if="shareGate" class="sub">You'll land on “{{shareGate.name}}” right after.</p>
      <!-- The default line is the SANDBOX's ("bring it into a real account"), which is nonsense to
           someone who arrived on a catalog page from a search result and has no sandbox. -->
      <p v-else-if="publicCatalog" class="sub">Verify your new account and create a team to call these tools. <b>$1.00 of credit once</b> on an eligible team, no card.</p>
      <p v-else class="sub">Sign in to connect your agent and manage your team.</p>
      <button v-if="meta.github" class="btn primary" style="width:100%;padding:11px;margin-bottom:8px" @click="githubLogin">Continue with GitHub</button>
      <button v-if="meta.google" class="btn" style="width:100%;padding:11px;margin-bottom:8px" @click="googleLogin">Continue with Google</button>
      <div v-if="!emailStage">
        <div class="field"><input v-model="emailInput" type="email" placeholder="you@work.com" @keyup.enter="emailStart"/></div>
        <button class="btn" style="width:100%" @click="emailStart" :disabled="busy">{{busy?'Sending…':'Email me a sign-in code'}}</button>
      </div>
      <div v-else>
        <p class="sub" style="font-size:11px;margin:2px 0 8px">Code sent to <b>{{emailInput}}</b><span v-if="devCode"> · dev code <b class="mono">{{devCode}}</b></span> · <a href="#" @click.prevent="emailStage=false">change</a></p>
        <div class="field"><input v-model="codeInput" inputmode="numeric" placeholder="6-digit code" @keyup.enter="emailVerify"/></div>
        <button class="btn primary" style="width:100%" @click="emailVerify" :disabled="busy">{{busy?'Verifying…':'Sign in'}}</button>
      </div>
      <details v-if="!oauthSignin" style="margin-top:12px;text-align:left">
        <summary class="sub" style="cursor:pointer;text-align:center">or paste an org token (agents &amp; CLI)</summary>
        <div class="field" style="margin-top:8px"><input v-model="tokenInput" type="password" placeholder="X-Treg-Token" @keyup.enter="addToken(tokenInput)"/></div>
        <button class="btn" style="width:100%" @click="addToken(tokenInput)" :disabled="busy">{{busy?'Checking…':'Sign in with token'}}</button>
      </details>
      <div v-if="loginErr" class="banner" style="margin-top:12px">{{loginErr}}</div>
    </div>
  </div>
</template>
