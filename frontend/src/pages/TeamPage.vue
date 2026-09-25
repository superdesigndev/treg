<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>

          <div class="tut-head">
            <div><h1>Team: {{activeName}}<span v-if="isPersonal(activeOrg)" class="chip" style="margin-left:8px;vertical-align:middle">personal</span></h1>
              <p class="sub" style="margin:0">You are <b>{{activeRole}}</b> here. <span class="muted">Switch, create or join a team from the picker at the top of the page.</span></p></div>
          </div>
          <!-- settings for the ACTIVE team (switching lives in the sidebar) -->
          <div v-if="activeOrg" style="margin-top:20px">
            <div class="tabs" style="margin:4px 0 16px">
              <button v-if="canAdmin" :class="{active:orgTab==='members'}" @click="orgTab='members'">Members</button>
              <button :class="{active:orgTab==='keys'}" @click="orgTab='keys'; loadApiKeys()">API Keys</button>
              <button v-if="canAdmin" :class="{active:orgTab==='projects'}" @click="orgTab='projects'">Projects</button>
              <button v-if="canAdmin" :class="{active:orgTab==='policy'}" @click="orgTab='policy'">Policy</button>
              <button v-if="canAdmin" :class="{active:orgTab==='billing'}" @click="orgTab='billing'">Billing</button>
              <button :class="{active:orgTab==='danger'}" @click="orgTab='danger'; resetRenameForm()">Team settings</button>
            </div>
            <p v-if="!canAdmin && !['keys','danger'].includes(orgTab)" class="sub" style="margin-bottom:12px">Members, projects and policy are visible to admins.</p>
            <p v-if="myUsage && myUsage.cap>=0" class="sub" style="margin:-6px 0 12px">Your usage today: <b>{{myUsage.used_today}} / {{myUsage.cap}}</b> calls.</p>
            <div v-if="orgErr" class="banner">{{orgErr}}</div>
            <div v-if="orgMsg" class="tut-notice" style="margin-bottom:12px;display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
              <span>{{orgMsg}}</span><button class="btn sm ico" @click="orgMsg=''" aria-label="Dismiss">✕</button>
            </div>

            <template v-if="canAdmin && orgTab==='members'">
              <div v-if="agentErr" class="banner" style="margin-top:8px">{{agentErr}}</div>
          <!-- the token is shown ONCE: make that unmissable -->
          <div v-if="newAgent || snipAgent" class="card" style="margin-top:16px" :style="newAgent?'border-color:var(--accent)':''">
            <div class="lbl" style="margin-top:0;display:flex;align-items:center;gap:10px">
              <template v-if="newAgent">Token for {{newAgent.name}} — copy it now</template>
              <template v-else>How to use {{snipAgent.name}}</template>
              <button v-if="!newAgent" class="btn sm" style="margin-left:auto" @click="snipAgent=null">Close</button>
            </div>
            <p v-if="newAgent && newAgent.rotated" class="tut-notice" style="margin:2px 0 10px"><b>The previous key stopped working immediately.</b> Update <b>{{newAgent.name}}</b> everywhere it runs before closing this card.</p>
            <p v-if="newAgent" class="sub" style="margin:2px 0 10px">This is the only time it is shown. Put it in the agent's <code>TREG_TOKEN</code>. Anyone holding it can act as {{newAgent.name}}, so keep it in an environment variable or secret manager — never commit or share it.
              <span v-if="agentConnected" class="chip ok" style="margin-left:8px">✓ connected — {{newAgent.name}} called in as itself</span>
              <span v-else class="muted" style="margin-left:8px">waiting for its first check-in…</span></p>
            <p v-else-if="agentTokens[snipAgent.user_id]" class="sub" style="margin:2px 0 10px">Snippets carry the real token (minted this page-load — the server only keeps a hash).</p>
            <p v-else class="sub" style="margin:2px 0 10px">The server stores only a hash of the token, so it can't be shown again — these snippets use <span class="mono">$TREG_TOKEN</span>. <button class="btn sm" @click="rotateAgent(snipAgent,true)">⟳ Rotate &amp; fill real token</button> <span class="muted">(the old token stops working)</span></p>
            <div v-if="newAgent" class="field">
              <input :value="newAgent.token" readonly style="font-family:var(--mono)" @focus="$event.target.select()"/>
              <button class="btn primary" @click="copy(newAgent.token,'agenttok')">{{copied==='agenttok'?'Copied':'Copy'}}</button>
              <button class="btn" @click="newAgent=null">{{newAgent.rotated?'I’ve updated '+newAgent.name:'Done'}}</button>
            </div>
            <!-- a bare token is a dead end: hand over the paste-ready thing, like the Tools snippets do -->
            <div class="tabs" style="margin:14px 0 8px">
              <button :class="{active:agentSnip==='prompt'}" @click="agentSnip='prompt'">Give it to your agent</button>
              <button :class="{active:agentSnip==='env'}" @click="agentSnip='env'">Environment</button>
            </div>
            <div class="lc-codewrap">
              <button class="lc-cp" @click="copy(agentSnippet,'agentsnip')">{{copied==='agentsnip'?'✓ copied':'copy'}}</button>
              <pre style="max-height:220px;overflow:auto">{{agentSnippet}}</pre>
            </div>
            <p class="sub" style="margin:6px 0 0;font-size:11.5px">Put this wherever the agent runs — a <span class="mono">.env</span>, a CI secret, or its config.</p>
          </div>


              <div class="lbl" style="margin-top:14px">Members</div>
              <p class="sub" style="margin:-2px 0 8px;font-size:12px">Daily cap = how many calls + runs a member may make per day. <b>-1</b> = unlimited.</p>
              <p class="sub" style="margin:-2px 0 8px;font-size:12px"><b>Tools</b> = which endpoints/CLIs a member may call or run. <b>Local run</b> = may run CLIs on their own machine (off = server only). The owner always has full access.</p>
              <table>
                <tr><th>Email</th><th>Role</th><th style="text-align:right">Today</th><th>Daily cap</th><th>Tools</th><th>Local run</th><th></th></tr>
                <!-- An empty roster under its header read as "no members" until the request answered. -->
                <tr v-if="!orgMembersLoaded"><td colspan="7" class="muted">Loading members…</td></tr>
                <template v-for="m in rosterMembers" :key="m.key">
                <tr v-if="m.is_observed">
                  <td><span style="opacity:.45">↳</span> <b>{{m.client}}</b> <span class="chip" title="seen in this member\'s traffic — the runtime reports itself; attribution, not authentication">detected</span>
                    <div class="muted" style="font-size:11px">runs as {{short(m.member)}} — inherits their full access</div></td>
                  <td><span class="muted" style="font-size:12px">—</span></td>
                  <td style="text-align:right" class="muted">{{m.used_today}}</td>
                  <td colspan="3"><span class="muted" style="font-size:12px">{{m.calls_30d}} calls in 30 days · last seen {{when(m.last_seen)}}</span></td>
                  <td style="text-align:right"><button class="btn sm" @click="promoteObserved(m)" title="mint this runtime its own token — its own cap, scope and audit trail">Scope this agent</button></td>
                </tr>
                <tr v-else>
                  <td>
                    <template v-if="m.is_agent"><span style="opacity:.45">↳</span> <b>{{m.name}}</b> <span class="chip" :title="m.email">agent</span>
                      <div class="muted" style="font-size:11px">{{m.created_by?('owned by '+short(m.created_by)):'machine identity'}}</div>
                      <div v-if="confirmAgent==='rotate-'+m.user_id" class="sub" style="font-size:11px;margin-top:5px;max-width:360px"><b>Its current key will stop immediately.</b> The next card shows the replacement once so you can update {{m.name}}.</div>
                      <div v-if="confirmAgent==='revoke-'+m.user_id" class="sub" style="font-size:11px;margin-top:5px;max-width:360px"><b>This removes the agent and revokes all its keys.</b> Activity stays for audit; restoring it means creating and configuring it again.</div></template>
                    <template v-else>{{m.email}}</template>
                  </td>
                  <td>
                    <select v-if="isOwner" class="msel" :value="m.role" @change="setRole(m,$event.target.value)">
                      <option v-for="r in ['viewer','member','admin','owner']" :key="r" :value="r">{{r}}</option>
                    </select>
                    <span v-else class="role" :class="m.role">{{m.role}}</span>
                  </td>
                  <td style="text-align:right" class="muted">{{m.used_today}}</td>
                  <td><input class="msel" type="number" min="-1" step="1" style="width:78px" :value="m.daily_call_cap" @change="setCap(m,$event.target.value)" title="-1 = unlimited"/></td>
                  <td>
                    <span v-if="m.role==='owner'" class="chip">All</span>
                    <button v-else class="btn sm" :class="{active:editAccess===m.user_id}" @click="openAccess(m)" :title="m.tool_access===null?'Access to every tool':'Access to '+m.tool_access.length+' tool(s)'">{{m.tool_access===null?'All':m.tool_access.length+' tools'}} ▾</button>
                  </td>
                  <td>
                    <label class="tgl" :title="m.role==='owner'?'the owner always may run locally':(m.local_run_enabled?'local runs on — click to disable':'local runs off — server only')">
                      <input type="checkbox" :checked="m.local_run_enabled" :disabled="m.role==='owner'" @change="setLocalRun(m,$event.target.checked)"/>
                      <span>{{m.local_run_enabled?'on':'off'}}</span>
                    </label>
                  </td>
                  <td style="text-align:right;white-space:nowrap">
                    <template v-if="m.is_agent">
                      <span class="row-actions"><button class="btn sm" @click="showAgentSetup(m)" title="how to give this agent its identity">Setup</button>
                      <button class="btn sm" :class="{danger:confirmAgent==='rotate-'+m.user_id}" @click="rotateAgent(m)" title="issue a new token — the old one stops working immediately">{{confirmAgent==='rotate-'+m.user_id?'Confirm rotate':'Rotate'}}</button>
                      <button class="btn sm" :class="{danger:confirmAgent==='revoke-'+m.user_id}" @click="revokeAgent(m)">{{confirmAgent==='revoke-'+m.user_id?'Confirm revoke':'Revoke'}}</button></span>
                    </template>
                    <button v-else-if="m.role!=='owner'" class="btn sm" :class="{danger:confirmRemove===m.user_id}" @click="removeMember(m)">{{confirmRemove===m.user_id?'Confirm remove':'Remove'}}</button>
                  </td>
                </tr>
                <tr v-if="!m.is_observed && editAccess===m.user_id">
                  <td colspan="7" style="background:color-mix(in srgb,var(--accent) 5%,transparent)">
                    <div style="padding:6px 2px">
                      <div class="sub" style="margin-bottom:6px">Tools &amp; skills <b>{{m.email}}</b> may use/see — uncheck to withhold. All checked = full access (and new ones apply automatically).</div>
                      <div v-if="!accessNames.length" class="muted" style="font-size:12px">No tools registered yet.</div>
                      <div style="display:flex;flex-wrap:wrap;gap:6px 16px">
                        <label v-for="n in accessNames" :key="n" class="tgl" style="min-width:150px">
                          <input type="checkbox" v-model="accessDraft[n]"/><span>{{n}}</span>
                        </label>
                      </div>
                      <template v-if="projects.length">
                        <div class="sub" style="margin:12px 0 6px">Projects <b>{{m.email}}</b> may use — all checked = every project (and new ones apply automatically). Tools with no project stay visible to everyone.</div>
                        <div style="display:flex;flex-wrap:wrap;gap:6px 16px">
                          <label v-for="p in projects" :key="p.id" class="tgl" style="min-width:150px">
                            <input type="checkbox" v-model="projDraft[p.id]"/><span>{{p.name}}</span>
                          </label>
                        </div>
                      </template>
                      <p v-else class="sub" style="margin:12px 0 0;font-size:12px">Tip: create a <a href="#" @click.prevent="orgTab='projects'">project</a> to scope members to just part of the team's tools.</p>
                      <div style="margin-top:10px;display:flex;gap:8px">
                        <button class="btn sm primary" @click="saveAccess(m)">Save access</button>
                        <button class="btn sm" @click="editAccess=null">Cancel</button>
                        <button class="btn sm" @click="setAllAccess(true)">Check all</button>
                        <button class="btn sm" @click="setAllAccess(false)">Uncheck all</button>
                      </div>
                    </div>
                  </td>
                </tr>
                </template>
              </table>

              <div class="lbl" style="margin-top:22px;display:flex;align-items:center;gap:10px">Add to this team
                <button v-if="!isPersonal(activeOrg)" class="btn sm" :class="{active:showInvite}" @click="showInvite=!showInvite; if(showInvite)showAddAgent=false">＋ Add member</button>
                <button class="btn sm" :class="{active:showAddAgent}" @click="openAddAgent()">＋ Add agent</button></div>
              <p class="sub" style="margin:-2px 0 8px;font-size:12px">A member is a person who signs in. An agent is a machine identity with its own token — capped, scoped and logged as itself. Scope either with <b>Tools</b> and <b>Projects</b> above.</p>
              <div v-show="showAddAgent">
          <div class="lbl" style="margin-top:18px">New agent</div>
          <p v-if="promoteHint" class="sub" style="margin:-2px 0 6px;font-size:12px;color:var(--accent)">{{promoteHint}}</p>
          <div class="field" style="max-width:620px">
            <input v-model="agentName" placeholder="ci-bot" @keyup.enter="createAgent"/>
            <select v-model="agentRole" class="msel"><option>viewer</option><option>member</option><option v-if="isOwner">admin</option></select>
            <input v-model.number="agentCap" type="number" min="-1" step="1" class="msel" style="width:96px" title="daily call cap (-1 = unlimited)"/>
            <button class="btn primary" @click="createAgent" :disabled="agentBusy||!agentAccessMode">{{agentBusy?'…':'Create'}}</button>
          </div>
          <div style="max-width:620px;margin:4px 0 8px">
            <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
              <span class="sub" style="font-size:12px"><b>Tools:</b></span>
              <label class="tgl"><input type="radio" value="all" v-model="agentAccessMode"/><span>All tools</span></label>
              <label class="tgl"><input type="radio" value="choose" v-model="agentAccessMode"/><span>Choose tools</span></label>
              <span v-if="!agentAccessMode" class="sub" style="font-size:11px;color:var(--accent)">Choose access before creating.</span>
            </div>
            <div v-if="agentAccessMode==='choose'" style="display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:7px">
              <span v-if="!accessNames.length" class="muted" style="font-size:12px">No tools registered yet; this agent will have no tool access.</span>
              <label v-for="n in accessNames" :key="n" class="tgl" style="min-width:150px"><input type="checkbox" v-model="agentToolSel[n]"/><span>{{n}}</span></label>
            </div>
          </div>
          <div v-if="projects.length" style="max-width:620px;margin:4px 0 0;display:flex;gap:6px 16px;flex-wrap:wrap;align-items:center">
            <span class="sub" style="font-size:12px">Projects (all checked = every project):</span>
            <label v-for="p in projects" :key="p.id" class="tgl"><input type="checkbox" v-model="agentProjSel[p.id]"/><span>{{p.name}}</span></label>
          </div>
          <p class="sub" style="margin:-2px 0 0;font-size:12px">Cap <b>-1</b> = unlimited. An agent can never be an owner, and can never sign in — its token is the only way to act as it.</p>

              </div>
              <template v-if="!isPersonal(activeOrg)">
              <div v-show="showInvite">
              <div class="field" style="max-width:560px">
                <input v-model="inviteEmail" type="email" placeholder="teammate@email.com" @keyup.enter="sendInvite"/>
                <select v-model="inviteRole" class="msel"><option>viewer</option><option>member</option><option v-if="isOwner">admin</option></select>
                <button class="btn primary" @click="sendInvite" :disabled="orgBusy">{{orgBusy?'…':'Invite'}}</button>
              </div>
              <div style="max-width:560px;margin:-4px 0 4px;display:flex;gap:16px;align-items:center;flex-wrap:wrap">
                <label class="tgl"><input type="radio" :checked="!inviteCustomize" @change="inviteCustomize=false"/><span>All tools</span></label>
                <label class="tgl"><input type="radio" :checked="inviteCustomize" @change="openInviteCustomize()"/><span>Customize</span></label>
                <label class="tgl"><input type="checkbox" v-model="inviteLocalRun"/><span>Local runs allowed</span></label>
              </div>
              <div v-if="inviteCustomize" style="max-width:560px;margin-bottom:8px">
                <div class="sub" style="font-size:12px;margin-bottom:4px">Tools this member may use (all checked by default — uncheck to withhold):</div>
                <div v-if="!tools.length" class="muted" style="font-size:12px">No tools registered yet.</div>
                <div style="display:flex;flex-wrap:wrap;gap:6px 16px">
                  <label v-for="n in accessNames" :key="n" class="tgl" style="min-width:150px"><input type="checkbox" v-model="inviteToolSel[n]"/><span>{{n}}</span></label>
                </div>
              </div>
              <div v-if="lastInvite" class="tut-notice" style="max-width:560px">
                Invite sent to <b>{{lastInvite.email}}</b> ({{lastInvite.role}}). They can sign in and accept it code-free - or use this one-time code:
                <div style="display:flex;gap:8px;align-items:center;margin-top:6px"><code class="mono" style="word-break:break-all;font-size:11px">{{lastInvite.code}}</code><button class="btn sm" @click="copy(lastInvite.code)">{{copied?'✓ copied':'⧉ copy'}}</button></div>
              </div>

              <div class="lbl" style="margin-top:18px">Pending invites</div>
              <table v-if="orgInvites.length">
                <tr><th>Email</th><th>Role</th><th>Invited by</th><th>Expires</th><th></th></tr>
                <tr v-for="inv in orgInvites" :key="inv.id">
                  <td>{{inv.email}}</td><td><span class="role" :class="inv.role">{{inv.role}}</span></td>
                  <td class="muted">{{short(inv.invited_by)}}</td><td class="muted">{{inv.expires_at?until(inv.expires_at):'never'}}</td>
                  <td style="text-align:right"><button class="btn sm" @click="revokeInvite(inv)">Revoke</button></td>
                </tr>
              </table>
              <p v-else class="sub">No pending invites.</p>
              </div>
              </template>
            </template>

            <!-- API KEYS tab. Members see their own keys; admins see the full team inventory. -->
            <template v-if="orgTab==='keys'">
              <div class="tut-head" style="align-items:flex-end;margin-bottom:14px"><div><div class="lbl">API Keys</div>
                <p class="sub" style="margin:2px 0 0">Use more than one key without making a new team member. Permissions, limits, and billing stay with the assigned identity.</p></div></div>
              <div v-if="keyErr" class="banner">{{keyErr}}</div>
              <div v-if="keyMsg" class="tut-notice" style="margin-bottom:12px;display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
                <span>{{keyMsg.text}}</span><span style="white-space:nowrap"><button v-if="keyMsg.agent" class="btn sm" @click="orgTab='members'; keyMsg=null">Open Members</button><button class="btn sm ico" @click="keyMsg=null" aria-label="Dismiss">✕</button></span>
              </div>
              <div v-if="newApiKey" class="card" style="border-color:var(--accent);margin-bottom:16px">
                <div class="lbl"><template v-if="newApiKey.kind==='default_human'">Default key rotated — copy the new key</template><template v-else-if="newApiKey.assigned_type==='agent'">New key for {{newApiKey.assigned_name}}</template><template v-else>{{newApiKey.name}} — copy it now</template></div>
                <p v-if="newApiKey.kind==='default_human'" class="tut-notice" style="margin:2px 0 10px"><b>The previous key stopped working immediately.</b> Update every CLI, MCP installation, or other client using this team's Default key.</p>
                <p v-if="newApiKey.assigned_type==='agent' && newApiKey.rotated" class="tut-notice" style="margin:2px 0 10px"><b>The previous key stopped working immediately and is now hidden from this list.</b> Update {{newApiKey.assigned_name}} everywhere it runs before closing this card.</p>
                <p class="sub" style="margin:2px 0 10px"><template v-if="newApiKey.kind==='default_human'">This team-specific key remains revealable on Getting Started.</template><template v-else>This is the only time treg shows the full key. The server stores only its hash.<template v-if="newApiKey.assigned_type==='agent'"> Anyone holding it can act as {{newApiKey.assigned_name}}, so keep it in an environment variable or secret manager — never commit or share it.</template></template></p>
                <div class="field"><input :value="newApiKey.secret" readonly class="mono" @focus="$event.target.select()"/><button class="btn primary" @click="copy(newApiKey.secret,'managedkey')">{{copied==='managedkey'?'Copied':'Copy'}}</button><button class="btn" @click="newApiKey=null">{{newApiKey.assigned_type==='agent'?'I’ve updated '+newApiKey.assigned_name:'Done'}}</button></div>
                <p v-if="newApiKey.kind==='default_human'" class="sub" style="margin:6px 0 0;font-size:11.5px">CLI users can run <code>treg login</code> again to save the new key. Update MCP and other clients manually.</p>
                <template v-if="newApiKey.assigned_type==='agent'">
                  <div class="tabs" style="margin:14px 0 8px"><button :class="{active:agentSnip==='prompt'}" @click="agentSnip='prompt'">Give it to your agent</button><button :class="{active:agentSnip==='env'}" @click="agentSnip='env'">Environment</button></div>
                  <div class="lc-codewrap"><button class="lc-cp" @click="copy(agentSnippet,'agentsnip')">{{copied==='agentsnip'?'✓ copied':'copy'}}</button><pre style="max-height:220px;overflow:auto">{{agentSnippet}}</pre></div>
                  <p class="sub" style="margin:6px 0 0;font-size:11.5px">Replace <code>TREG_TOKEN</code> in every environment, CI secret, secret manager, or agent config where {{newApiKey.assigned_name}} runs.</p>
                </template>
              </div>
              <div v-if="activeRole!=='viewer'" class="field" style="max-width:620px;margin-bottom:18px"><input v-model="keyName" :class="{'field-invalid':keyNameInvalid}" :aria-invalid="keyNameInvalid" placeholder="New key name" maxlength="80" @input="keyNameInvalid=false" @keyup.enter="createApiKey"/><button class="btn primary" :disabled="keyBusy" @click="createApiKey">{{keyBusy?'…':'Create key'}}</button></div>
              <template v-for="group in apiKeyGroups" :key="group.identity">
                <div class="lbl" style="margin-top:16px">{{group.name}} <span class="chip">{{group.type}}</span><span v-if="group.type==='agent' && group.name!==group.identity" class="muted mono" style="margin-left:8px;text-transform:none">{{group.identity}}</span></div>
                <table class="key-table"><tr><th>Key</th><th>Status</th><th>Created</th><th>Last used</th><th></th></tr>
                  <tr v-for="k in group.rows" :key="k.id">
                    <td class="key-identity"><input v-if="editKey===k.id" v-model="editKeyName" maxlength="80" class="msel" @keyup.enter="renameApiKey(k)"/><template v-else><div class="key-primary"><span v-if="group.type==='human' && k.assigned_type==='agent'" class="muted" aria-hidden="true">↳</span><b>{{group.type==='human' && k.assigned_type==='agent'?k.assigned_name:k.name}}</b><span class="chip">{{keyKind(k.kind)}}</span></div><div class="key-meta mono">{{maskedKey(k)}}</div></template>
                    </td>
                    <td><span class="badge" :class="k.state==='active'?'ok':'invalid'">{{k.state}}</span></td><td class="muted">{{k.created_at?when(k.created_at):'Unknown'}}</td><td class="muted">{{k.last_used_at?when(k.last_used_at):'Never'}}</td>
                    <td style="text-align:right;white-space:nowrap"><span v-if="editKey===k.id" class="row-actions"><button class="btn sm primary" @click="renameApiKey(k)">Save</button><button class="btn sm" @click="editKey=null">Cancel</button></span><span v-else class="row-actions">
                      <button class="btn sm" @click="showKeyActivity(k)">Activity</button><button v-if="k.can_rotate" class="btn sm" @click="requestKeyAction(k,'rotate')">Rotate</button><button v-if="keyHasMore(k)" class="btn sm ico" aria-haspopup="menu" :aria-expanded="keyMenu&&keyMenu.key.id===k.id" :aria-label="'More actions for '+k.name" @click.stop="toggleKeyMenu(k,$event)">⋮</button>
                    </span></td>
                  </tr>
                </table>
              </template>
              <p v-if="!keyBusy && !apiKeys.length" class="sub">No keys are available.</p>
            </template>

            <!-- PROJECTS tab -->
            <!-- BILLING. Balance + top-ups + auto top-up for the ACTIVE team. Admin-gated like the
                 rest of the team tabs. Hidden entirely when the deployment has no Stripe key
                 (self-hosters don't sell balance) - `billing.configured` says so. -->
            <template v-if="canAdmin && orgTab==='billing'">
              <div v-if="billing" style="margin-top:4px">
                <div class="card" style="padding:16px">
                  <div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap">
                    <div style="flex:1;min-width:220px">
                      <div class="lbl">Balance</div>
                      <div style="font-size:28px;font-weight:700;color:var(--accent);font-family:var(--mono)">{{money(billing.balance_micro)}}</div>
                    </div>
                    <!-- Stripe's hosted portal: card, billing address, tax ID and the invoice archive.
                         Hidden until the team has a Stripe customer, which its first payment creates. -->
                    <button v-if="billing.portal" class="btn sm" :disabled="billingBusy" @click="openPortal()"
                            title="Update your card, billing address and tax ID, and download past invoices">
                      {{billingBusy?'Opening…':'Manage billing'}}
                    </button>
                  </div>
                  <p v-if="!billing.configured" class="sub" style="margin:12px 0 0">Top-ups aren't configured on this deployment.</p>

                  <template v-if="billing.configured">
                    <div style="margin-top:12px">
                      <!-- A team that arrived through someone's link does not know a bonus exists.
                           This is the one screen where saying so changes what they do, and the
                           MINIMUM is the part that matters: the first preset ($5) is below it, so
                           without this the most-clicked button silently forfeits the reward. -->
                      <div v-if="billing.referral_offer" style="margin-top:8px;padding:9px 12px;font-size:12.5px;
                           border:1px solid var(--green);border-radius:8px;
                           background:color-mix(in srgb,var(--green) 12%,transparent)">
                        <!-- The referrer is MASKED (`j•••@domain`): a referral link is public, so
                             the full address would be published to every stranger who signs up
                             through an influencer's link. The domain is what a real friend
                             recognises. Falls back to "to treg" if we somehow have no address. -->
                        You were invited
                        <template v-if="billing.referral_offer.referrer_masked">by
                          <b>{{billing.referral_offer.referrer_masked}}</b></template>
                        <template v-else>to treg</template>
                        — add
                        <!-- Once they have paid something, ask for the REMAINDER. Repeating the
                             full amount reads as though the money they already added did not
                             count, and it does: the threshold is cumulative. -->
                        <b>{{money(billing.referral_offer.remaining_micro)}}</b>
                        <template v-if="billing.referral_offer.topped_up_micro"> more</template>
                        <template v-else> or more</template>
                        <!-- "straight away" is load-bearing. The referee has no Referrals page, so
                             the balance is their ONLY feedback; promising a bonus without saying
                             when is what made a correct payout look like a failure. -->
                        and we'll add <b>{{money(billing.referral_offer.referred_micro)}}</b> to your
                        balance <b>straight away</b>. They get
                        {{money(billing.referral_offer.referrer_micro)}} after
                        {{billing.referral_offer.hold_days}} days.
                      </div>
                      <!-- The amounts live in a modal (openTopup), not inline: choosing a size,
                           seeing the bonus it earns and deciding on auto top-up are one decision,
                           and the mandate text has to sit next to the Pay button it authorizes. -->
                      <button class="btn primary" :disabled="billingBusy" @click="openTopup()">Top up</button>
                    </div>

                    <!-- `no_card` is not a failure: consent was recorded (usually from the top-up
                         modal) and the policy arms itself when a payment saves a card. Showing it in
                         the red "switched itself off" banner made an abandoned Checkout look broken. -->
                    <p v-if="billing.autotopup.disabled_reason==='no_card'" class="sub" style="margin:14px 0 0">
                      Auto top-up is set up and will switch on as soon as a top-up saves your card.
                    </p>
                    <div v-else-if="billing.autotopup.disabled_reason" class="banner" style="margin:14px 0 0">
                      Auto top-up switched itself off ({{billing.autotopup.disabled_reason}}). Once the balance runs out, calls will fail until it's funded.
                    </div>
                    <div style="margin-top:16px;border-top:1px solid var(--line);padding-top:14px">
                      <label style="display:flex;gap:10px;align-items:center;cursor:pointer">
                        <span class="tswitch"><input type="checkbox" :checked="billing.autotopup.enabled||autoOpen" :disabled="billingBusy" @change="autoToggled"/><span class="knob"></span></span>
                        <span class="lbl" style="margin:0">Auto top-up</span>
                        <span v-if="billing.card_on_file" class="sub" style="margin-left:auto">Card on file ✓</span>
                      </label>
                      <p class="sub" style="margin:8px 0 0">
                        <template v-if="billing.autotopup.enabled">On - we add {{money(billing.autotopup.amount_micro)}} whenever the balance drops below {{money(billing.autotopup.threshold_micro)}}. This month: {{money(billing.autotopup.month_spend_micro)}}.
                          <a href="#" @click.prevent="autoOpen=!autoOpen;autoConsent=false">{{autoOpen?'Cancel':'Edit'}}</a></template>
                        <template v-else>Keep agents running without watching the balance. We charge your saved card only when it dips below your threshold.</template>
                      </p>
                      <!-- The same panel edits a running policy (Edit link above) and arms a new one:
                           either way the numbers change, so the mandate is re-agreed and re-stamped. -->
                      <div v-if="autoOpen" style="margin-top:12px;background:var(--panel2);border:1px solid var(--line);border-radius:var(--rb);padding:12px 14px;max-width:560px">
                        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                          <span class="sub" style="margin:0">Add $</span>
                          <input class="msel" style="width:70px" type="number" min="5" v-model.number="autoAmount"/>
                          <span class="sub" style="margin:0">when the balance drops below $</span>
                          <input class="msel" style="width:70px" type="number" min="5" v-model.number="autoThreshold"/>
                        </div>
                        <label style="display:flex;gap:8px;align-items:flex-start;margin-top:12px;cursor:pointer">
                          <input type="checkbox" v-model="autoConsent" style="margin-top:3px"/>
                          <!-- The mandate text. Not decoration: an off-session charge with no recorded
                               agreement to THESE numbers is an unauthorized charge under PSD2/SCA. -->
                          <span class="sub" style="margin:0">I authorize treg to charge my saved card ${{autoAmount}} automatically whenever my balance drops below ${{autoThreshold}}. Cancel any time.</span>
                        </label>
                        <div style="margin-top:12px">
                          <button class="btn primary" :disabled="!autoConsent||billingBusy" @click="setAuto(true)">{{billingBusy?'…':(billing.autotopup.enabled?'Save':'Turn on auto top-up')}}</button>
                        </div>
                      </div>
                    </div>

                    <div style="margin-top:16px;border-top:1px solid var(--line);padding-top:14px">
                      <div class="lbl">Payment history</div>
                      <p v-if="bhist.loading" class="sub" style="margin:8px 0 0">Loading…</p>
                      <p v-else-if="!bhist.items.length" class="sub" style="margin:8px 0 0">No top-ups yet. Once you add funds, every payment shows up here with its invoice.</p>
                      <template v-else>
                        <!-- Amounts come from our own ledger, so this list always agrees with the
                             balance above. Only the links come from Stripe — hence the notice below. -->
                        <table style="width:100%;margin-top:8px;border-collapse:collapse;max-width:560px">
                          <tr v-for="i in bhist.items" :key="i.payment_intent" style="border-top:1px solid var(--line)">
                            <td style="padding:8px 10px 8px 0;white-space:nowrap">{{bhistDate(i.created_at)}}</td>
                            <td style="padding:8px 10px 8px 0;font-family:var(--mono);white-space:nowrap">{{money(i.amount_micro)}}</td>
                            <td style="padding:8px 10px 8px 0" class="sub">{{i.auto?'auto':''}}<span v-if="i.bonus_micro" style="color:var(--green)">+{{money(i.bonus_micro)}} bonus</span></td>
                            <td style="padding:8px 0;text-align:right;white-space:nowrap">
                              <a v-if="bhistLink(i)" :href="bhistLink(i)" target="_blank" rel="noopener">{{bhistLabel(i)}}</a>
                              <span v-else class="muted">—</span>
                            </td>
                          </tr>
                        </table>
                        <p v-if="!bhist.ok" class="sub" style="margin:8px 0 0">Invoice links are temporarily unavailable — the amounts above are correct. Try again shortly, or use <b>Manage billing</b> for the full archive.</p>
                      </template>
                    </div>
                  </template>

                </div>
              </div>
              <p v-else class="sub">Loading…</p>
            </template>

            <template v-if="canAdmin && orgTab==='projects'">
              <div class="lbl" style="margin-top:14px">Projects</div>
              <p class="sub" style="margin:-2px 0 8px;font-size:12px">A project groups tools inside this team, so a member or agent can be scoped to just part of it. A tool with no project stays visible to everyone. Secrets stay team-level — one shared credential can back tools in several projects; scope who <i>uses</i> them via member access.</p>
              <div class="field" style="max-width:560px">
                <input v-model="projectName" placeholder="Apollo" @keyup.enter="createProject"/>
                <button class="btn primary" @click="createProject" :disabled="projBusy">{{projBusy?'…':'Add project'}}</button>
              </div>
              <p v-if="!projects.length" class="muted" style="font-size:12px;margin-top:8px">No projects yet — every tool is team-wide.</p>
              <table v-else style="margin-top:8px">
                <tr><th>Project</th><th>Slug</th><th style="text-align:right">Tools</th><th></th></tr>
                <template v-for="p in projects" :key="p.id">
                <tr>
                  <td><b>{{p.name}}</b></td>
                  <td class="muted" style="font-family:var(--mono);font-size:12px">{{p.slug}}</td>
                  <td style="text-align:right">
                    <button class="btn sm" :class="{active:editProj===p.id}" @click="openProjTools(p)">{{p.tool_count}} tool{{p.tool_count===1?'':'s'}} ▾</button>
                  </td>
                  <td style="text-align:right">
                    <button class="btn sm" :class="{danger:confirmProj===p.id}" @click="deleteProject(p)" :title="'its '+p.tool_count+' tool(s) become team-wide — they are not deleted'">{{confirmProj===p.id?'Confirm delete':'Delete'}}</button>
                  </td>
                </tr>
                <tr v-if="editProj===p.id">
                  <td colspan="4" style="background:color-mix(in srgb,var(--accent) 5%,transparent)">
                    <div style="padding:6px 2px">
                      <div class="sub" style="margin-bottom:6px">Tools in <b>{{p.name}}</b> — check to add, uncheck to make team-wide again. Checking a tool that lives in another project moves it here.</div>
                      <div v-if="!tools.length" class="muted" style="font-size:12px">No tools registered yet.</div>
                      <div style="display:flex;flex-wrap:wrap;gap:6px 16px">
                        <label v-for="t in tools" :key="t.id" class="tgl" style="min-width:190px">
                          <input type="checkbox" v-model="projToolDraft[t.id]"/><span>{{t.name}}</span>
                          <span v-if="t.project_id && t.project_id!==p.id" class="chip" style="margin-left:4px" :title="'currently in '+projName(t.project_id)">{{projName(t.project_id)}}</span>
                        </label>
                      </div>
                      <div style="margin-top:10px;display:flex;gap:8px">
                        <button class="btn sm primary" @click="saveProjTools(p)" :disabled="projToolBusy">{{projToolBusy?'…':'Save'}}</button>
                        <button class="btn sm" @click="editProj=null">Cancel</button>
                      </div>
                    </div>
                  </td>
                </tr>
                </template>
              </table>

            </template>

            <!-- POLICY tab -->
            <template v-if="canAdmin && orgTab==='policy'">
              <div class="lbl" style="margin-top:14px">Policy</div>
              <p class="sub" style="margin:-2px 0 8px;font-size:12px">Block calls to a host, a path, or a method. Leave a field empty to mean <b>any</b>. Applies to the proxy and to runs — and to everyone, including owners.</p>
              <div class="field" style="max-width:720px;flex-wrap:wrap">
                <input v-model="denyForm.host" placeholder="host (e.g. api.stripe.com)" style="min-width:190px"/>
                <input v-model="denyForm.path_prefix" placeholder="path (e.g. /admin)" style="min-width:140px"/>
                <select v-model="denyForm.method" class="msel"><option value="">any method</option><option>GET</option><option>POST</option><option>PUT</option><option>PATCH</option><option>DELETE</option></select>
                <select v-model="denyForm.user_id" class="msel" title="who it applies to">
                  <option :value="null">whole team</option>
                  <option v-for="m in rosterMembers.filter(x=>!x.is_observed)" :key="m.user_id" :value="m.user_id">{{m.is_agent?('agent: '+(m.name||m.email)):m.email}}</option>
                </select>
                <select v-if="projects.length" v-model="denyForm.project_id" class="msel" title="which project's tools it applies to">
                  <option :value="null">any project</option>
                  <option v-for="p in projects" :key="p.id" :value="p.id">{{p.name}}</option>
                </select>
                <input v-model="denyForm.note" placeholder="why (shown in the refusal)" style="min-width:170px"/>
                <button class="btn primary" @click="addDeny" :disabled="denyBusy">{{denyBusy?'…':'Block'}}</button>
              </div>
              <p v-if="!denyRules.length" class="muted" style="font-size:12px;margin-top:8px">No rules — nothing is blocked.</p>
              <table v-else style="margin-top:8px">
                <tr><th>Method</th><th>Host</th><th>Path</th><th>Applies to</th><th v-if="projects.length">Project</th><th>Note</th><th></th></tr>
                <tr v-for="r in denyRules" :key="r.id">
                  <td><span class="chip">{{r.method||'any'}}</span></td>
                  <td style="font-family:var(--mono);font-size:12px">{{r.host||'any'}}</td>
                  <td style="font-family:var(--mono);font-size:12px">{{r.path_prefix||'any'}}</td>
                  <td>{{r.scope==='org'?'whole team':(denyWho(r.user_id))}}</td>
                  <td v-if="projects.length">{{r.project_id?projName(r.project_id):'any'}}</td>
                  <td class="muted" style="font-size:12px">{{r.note}}</td>
                  <td style="text-align:right"><button class="btn sm" :class="{danger:confirmDeny===r.id}" @click="removeDeny(r)">{{confirmDeny===r.id?'Confirm remove':'Remove'}}</button></td>
                </tr>
              </table>

              <!-- Tool-level argv blocks — the OTHER deny layer, surfaced read-only so the whole
                   "what is blocked" picture lives on one screen. -->
              <template v-if="cliDeny.length">
                <div class="lbl" style="margin-top:22px">Per-tool CLI blocks <span class="chip" style="margin-left:6px">read-only</span></div>
                <p class="sub" style="margin:-2px 0 8px;font-size:12px">treg blocks at three layers: the rules above (HTTP host/path/method, this team's policy), these per-tool argument patterns (from the skill's <span class="mono">treg.json</span> or the treg catalog — edit them there), and the OS sandbox around isolated local runs.</p>
                <table>
                  <tr><th>Tool</th><th>Blocked argument patterns</th></tr>
                  <tr v-for="t in cliDeny" :key="t.tool">
                    <td><b>{{t.tool}}</b><span v-if="!t.enabled" class="chip" style="margin-left:6px" title="local runs for this tool are not enabled">off</span></td>
                    <td><span v-for="pat in t.patterns" :key="pat.pattern" class="chip" style="margin:2px 6px 2px 0;font-family:var(--mono);font-size:11px" :title="pat.source==='skill'?'from this skill\'s treg.json':'from the treg catalog defaults'">{{pat.pattern}}<span class="muted" style="margin-left:4px">· {{pat.source}}</span></span></td>
                  </tr>
                </table>
              </template>
            </template>

            <!-- TEAM SETTINGS: just the danger zone (new/join/switch team live in the sidebar
                 picker). Visible to every role — leaving a team is self-service, not admin. -->
            <template v-if="orgTab==='danger'">
            <!-- DAILY SPEND CAP. A team-level setting, so it lives here rather than under Billing.
                 It was invisible entirely before this, which meant a team first met it as an
                 unexplained 429; and it matters MOST when auto top-up is on, because a balance that
                 refills itself is no longer a ceiling and this becomes the only thing bounding a
                 runaway agent. `capCfg` is loaded by loadBilling(), which runs on every Team-page
                 load — NOT per tab — so it is populated by the time this renders.
                 NB the name: `orgSettings` is already a METHOD on this component (the ⚙ in the team
                 picker), and data and methods share one namespace. -->
            <div v-if="capCfg" style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
                  <span class="lbl" style="margin:0">Daily spend limit</span>
                  <p class="sub" style="margin:4px 0 8px;font-size:12px">
                    The most this team can spend per day on treg's keys. Resets at 00:00 UTC. None by
                    default. Worth setting if agents run unattended with auto top-up on, since a
                    balance that refills itself can't stop a runaway one.
                  </p>
                  <div class="field" style="max-width:560px">
                    <input v-model="capUsd" type="number" min="0" step="0.5" placeholder="no limit" style="max-width:140px"/>
                    <button class="btn primary" :disabled="capBusy" @click="saveCap">{{capBusy?'…':'Save'}}</button>
                    <span class="sub" style="margin:0">
                      USD/day · now {{capCfg.daily_cap_micro ? money(capCfg.daily_cap_micro) : 'no limit'}}<template v-if="!capCfg.daily_cap_set_by_team"> (our default)</template>
                    </span>
                  </div>
                  <p v-if="capErr" class="banner" style="margin:10px 0 0">{{capErr}}</p>
                  <p class="muted" style="font-size:12px;margin-top:8px">
                    Any amount, up or down. Leave it empty (or 0) for no daily limit — your balance is then the only bound.
                  </p>
                </div>

                <!-- PER-TAG BUDGETS. What a team reselling treg sets on ITS OWN customers. Only
                     DECLARED dimensions are enforced on the call path, so the key is a picker over
                     those rather than a free-text box — the API refuses a limit on anything else
                     precisely because a stored-but-unenforced budget looks like protection and isn't. -->
                <div v-if="capCfg" style="margin-top:22px;border-top:1px solid var(--line);padding-top:14px">
                  <span class="lbl" style="margin:0">Per-customer limits</span>
                  <p class="sub" style="margin:4px 0 8px;font-size:12px">
                    Cap or block one of your own customers by the tags your calls carry
                    (<code>X-Treg-Meta</code>). Limits <b>stack</b> — a workspace ceiling and a
                    per-customer ceiling both apply, and a refusal names which one hit. They are
                    <b>advisory</b>: concurrent calls can overshoot slightly, and your balance is the
                    hard limit.
                  </p>
                  <div class="field" style="max-width:680px;flex-wrap:wrap">
                    <!-- Every tag the team SENDS, not only the ones already budgeted. Setting a
                         limit declares the dimension, so offering only the declared list made the
                         common path a hidden two-step: you can see `feature` in Usage, so you expect
                         to be able to cap it. -->
                    <select class="msel" v-model="budDim" style="max-width:160px">
                      <option v-for="k in budDims" :key="k" :value="k">{{k}}</option>
                    </select>
                    <input v-model="budVal" placeholder="cust_8123 — blank = default for all" style="max-width:230px"/>
                    <input v-model="budDaily" type="number" min="0" step="0.5" placeholder="USD/day" style="max-width:120px"/>
                    <button class="btn primary" :disabled="budBusy" @click="saveBudget()">{{budBusy?'…':(budVal?'Set limit':'Set default')}}</button>
                  </div>
                  <p v-if="budErr" class="banner" style="margin:10px 0 0">{{budErr}}</p>
                  <template v-for="dim in (capCfg.budget_dims||[])" :key="dim">
                    <table style="margin-top:10px">
                      <tr><th>{{dim}}</th><th style="text-align:right">Daily limit</th><th>Status</th><th></th></tr>
                      <!-- The default first: everything under it is an exception to it. Rendered even
                           when unset, so "no limit" is something you can SEE rather than infer from
                           an empty table. -->
                      <tr>
                        <td><b>every {{dim}}</b> <span class="muted">(default)</span></td>
                        <td style="text-align:right">{{defaultFor(dim)?fmtCap(defaultFor(dim)):'no limit'}}</td>
                        <td><span class="muted">—</span></td>
                        <td style="text-align:right"><button v-if="defaultFor(dim)" class="btn sm" @click="removeBudget(defaultFor(dim))">Clear</button></td>
                      </tr>
                      <tr v-for="b in overridesFor(dim)" :key="b.val">
                        <td style="padding-left:22px"><span class="chip">{{b.val}}</span></td>
                        <!-- EFFECTIVE, never a dash: an override with no cap really is unlimited, and
                             showing "—" would hide that it is exempt from the default. -->
                        <td style="text-align:right">{{fmtCap(b)}}</td>
                        <td><span class="badge" :class="b.status==='blocked'?'invalid':'ok'">{{b.status}}</span></td>
                        <td style="text-align:right;white-space:nowrap">
                          <button class="btn sm" @click="toggleBlock(b)">{{b.status==='blocked'?'Unblock':'Block'}}</button>
                          <button class="btn sm" @click="removeBudget(b)">Remove</button>
                        </td>
                      </tr>
                      <tr v-if="!overridesFor(dim).length"><td colspan="4" class="muted" style="font-size:12px">no exceptions — every {{dim}} follows the default</td></tr>
                    </table>
                  </template>
                </div>


            <!-- TEAM NAME + SLUG. Admin+. The old slug stays an alias server-side, so copied keys,
                 ~/.treg and MCP pins keep working; the CLI just needs `treg org use <new-slug>`. -->
            <div v-if="canAdmin && !isPersonal(activeOrg)" style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
              <span class="lbl" style="margin:0">Team name and slug</span>
              <p class="sub" style="margin:4px 0 8px;font-size:12px">
                The slug is the team's id in <code>X-Treg-Org</code>, <code>treg org use</code> and MCP. Changing it
                keeps existing keys working; run <code>treg org use {{renameSlug.trim()||activeSlugNow}}</code> to re-pin the CLI.
              </p>
              <div class="field" style="max-width:560px">
                <input v-model="renameName" placeholder="team name" maxlength="80" style="max-width:220px"/>
                <input v-model="renameSlug" placeholder="slug" maxlength="40" style="max-width:180px"/>
                <button class="btn primary" :disabled="renameBusy || !renameDirty" @click="renameOrg">{{renameBusy?'…':'Save'}}</button>
              </div>
              <p v-if="renameErr" class="sub" style="color:var(--red);margin:4px 0 0;font-size:12px">{{renameErr}}</p>
            </div>

            <template v-if="!isPersonal(activeOrg)">
            <div class="lbl" style="margin-top:14px;color:var(--red)">Danger zone</div>
            <div style="display:flex;gap:22px;flex-wrap:wrap;align-items:flex-start">
              <div>
                <button class="btn sm" :class="{danger:confirmLeave}" @click="leaveOrg">{{confirmLeave?'Confirm leave':'Leave org'}}</button>
                <a v-if="confirmLeave" href="#" @click.prevent="confirmLeave=false" class="sub" style="margin-left:8px;font-size:12px">cancel</a>
              </div>
              <div v-if="isOwner" class="field" style="margin:0">
                <input v-model="confirmDel" :placeholder="'type '+activeSlugNow+' to delete'" style="min-width:230px"/>
                <button class="btn sm danger" :disabled="confirmDel!==activeSlugNow" @click="deleteOrg">Delete org</button>
              </div>
            </div>
            </template>
            <p v-else class="sub" style="margin-top:14px;font-size:12px">A personal team cannot be left or deleted. Create or join other teams from the picker at the top of the page.</p>
            </template>
          </div>

</template>
