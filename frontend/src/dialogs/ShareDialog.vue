<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-modal="true"  @click.self="share.on=false">
      <div class="modal" style="width:min(560px,95vw)"><div class="hd"><b>Share “{{detail.name}}”</b><button class="btn sm ico" @click="share.on=false" aria-label="Close">✕</button></div>
        <div style="padding:16px 18px">
          <template v-if="share.sent">
            <p class="explain">✓ Invite sent to <b>{{share.sent.email}}</b> — the email's button signs them in and lands them right on this page.</p>
            <p class="sub" style="margin:0 0 8px">Or DM them this link — it opens the sign-in with their email prefilled; once they sign in, the invite accepts itself and this page opens:</p>
            <pre class="code" style="white-space:pre-wrap;word-break:break-all">{{shareInviteUrl}}</pre>
            <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
              <button class="btn primary" @click="copyDetail(shareInviteUrl,'sharecopy')">⧉ {{detailCopied==='sharecopy'?'Copied!':'Copy invite link'}}</button>
              <span class="sub" style="margin:0;font-size:12px">backup code: <span class="mono">{{share.sent.code}}</span> (<span class="mono">treg org join</span>)</span>
            </div>
          </template>
          <template v-else>
            <p class="explain">Already on the team? Just send them the page link (⧉ Copy link). This invites someone <b>new</b> — after one click in the email they land on this exact page.</p>
            <div v-if="share.member" class="tut-notice" style="margin:0 0 10px"><b>{{share.member.email}}</b> is already a member — just send them the link, no invite needed.</div>
            <div class="field" style="margin-bottom:10px"><input v-model="share.email" type="email" placeholder="teammate@company.com" @keyup.enter="sendShare"/></div>
            <div style="margin-bottom:10px">
              <label style="display:flex;gap:6px;align-items:center;font-size:12.5px">role
                <select v-model="share.role" style="background:var(--panel2);border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:4px 8px;font-family:var(--mono)">
                  <option value="viewer">viewer (use, not edit)</option><option value="member">member</option><option v-if="isOwner" value="admin">admin</option>
                </select>
              </label>
            </div>
            <div style="margin-bottom:10px">
              <label style="display:flex;gap:8px;align-items:center;font-size:12.5px;cursor:pointer">
                <input type="checkbox" v-model="share.full"/> Share full access
              </label>
            </div>
            <p class="sub" style="margin:0 0 12px;font-size:12px">Full access = they see and can use everything the team has registered. Uncheck to scope them to just {{detail.kind==='skill'?'this skill':'this tool'}} — they'll only see and call what you're sharing. Local CLI runs stay off either way.</p>
            <div v-if="share.err" class="banner" style="margin:0 0 10px">{{share.err}}</div>
            <button class="btn primary" @click="sendShare" :disabled="share.busy">{{share.busy?'Sending…':'Send invite'}}</button>
          </template>
        </div></div>
    </div>
</template>
