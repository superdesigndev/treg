<script>
import controller from './state/controller.js'
import TeamResourcesPage from './pages/TeamResourcesPage.vue'
import FishVoiceDialog from './dialogs/FishVoiceDialog.vue'
import CatalogPage from './pages/CatalogPage.vue'
import ProviderPage from './pages/ProviderPage.vue'
import PlatformPage from './pages/PlatformPage.vue'
import ToolsPage from './pages/ToolsPage.vue'
import DetailPage from './pages/DetailPage.vue'
import SecretsPage from './pages/SecretsPage.vue'
import TeamPage from './pages/TeamPage.vue'
import ActivityPage from './pages/ActivityPage.vue'
import AdminPage from './pages/AdminPage.vue'
import GettingStartedPage from './pages/GettingStartedPage.vue'
import ReferralsPage from './pages/ReferralsPage.vue'
import HelpPage from './pages/HelpPage.vue'
import SearchPage from './pages/SearchPage.vue'
import SignedOutPage from './components/SignedOutPage.vue'
import BrandMark from './components/BrandMark.vue'
import PublicNavigation from './components/PublicNavigation.vue'
import LandingNavigation from './components/LandingNavigation.vue'
import DashboardNavigation from './components/DashboardNavigation.vue'
import ConnectTokenDialog from './dialogs/ConnectTokenDialog.vue'
import TopUpDialog from './dialogs/TopUpDialog.vue'
import AgentGuideDialog from './dialogs/AgentGuideDialog.vue'
import ConnectionMethodDialog from './dialogs/ConnectionMethodDialog.vue'
import ResourcePickerDialog from './dialogs/ResourcePickerDialog.vue'
import ExtraCredentialDialog from './dialogs/ExtraCredentialDialog.vue'
import EditToolDialog from './dialogs/EditToolDialog.vue'
import AcceptInvitesDialog from './dialogs/AcceptInvitesDialog.vue'
import WelcomeDialog from './dialogs/WelcomeDialog.vue'
import CopyToolDialog from './dialogs/CopyToolDialog.vue'
import ImportSkillDialog from './dialogs/ImportSkillDialog.vue'
import RequestToolDialog from './dialogs/RequestToolDialog.vue'
import ShareDialog from './dialogs/ShareDialog.vue'
import RecipeDialog from './dialogs/RecipeDialog.vue'
import RunToolDialog from './dialogs/RunToolDialog.vue'
import CallDetailsDialog from './dialogs/CallDetailsDialog.vue'
import TryEndpointDialog from './dialogs/TryEndpointDialog.vue'
import SignInDialog from './components/SignInDialog.vue'
export default { ...controller, components: { ...controller.components, TeamResourcesPage, FishVoiceDialog, CatalogPage, ProviderPage, PlatformPage, ToolsPage, DetailPage, SecretsPage, TeamPage, ActivityPage, AdminPage, GettingStartedPage, ReferralsPage, HelpPage, SearchPage, SignedOutPage, BrandMark, PublicNavigation, LandingNavigation, DashboardNavigation, ConnectTokenDialog, TopUpDialog, AgentGuideDialog, ConnectionMethodDialog, ResourcePickerDialog, ExtraCredentialDialog, EditToolDialog, AcceptInvitesDialog, WelcomeDialog, CopyToolDialog, ImportSkillDialog, RequestToolDialog, ShareDialog, RecipeDialog, RunToolDialog, CallDetailsDialog, TryEndpointDialog, SignInDialog } }
</script>

<template>
<div>
<main v-if="!bootReady || bootFailed" class="boot-status" aria-live="polite" :aria-busy="!bootReady">
  <a href="/" class="brand"><BrandMark/>treg</a>
  <template v-if="bootFailed">
    <p role="alert">The dashboard couldn't load. Please try again.</p>
    <button class="btn" @click="reloadApp()">Try again</button>
  </template>
  <p v-else role="status">Loading treg…</p>
</main>
<div v-else :class="{redesign:authed && !publicCatalog}">
  <!-- Focused sign-in entry after session initialization. -->
  <SignedOutPage v-if="!authed && !publicCatalog" />

  <template v-else>
    <a href="#maincontent" class="skip">Skip to content</a>
    <!-- A public catalog visitor is reading a WEBSITE, not operating an app: the workspace chrome
         (org switcher, global tool search, member nav) is furniture for a job they have not started.
         They get the marketing site's nav instead, so /catalog reads as part of treg.to rather than
         as a dashboard someone forgot to lock. -->
    <LandingNavigation v-if="view==='find'" />
    <PublicNavigation v-else-if="publicCatalog" />
    <DashboardNavigation v-else />
    <img v-if="authed && !publicCatalog && view==='start'" class="rd-background" src="/media/redesign/ascii-background.jpg" alt="" aria-hidden="true">
    <div v-if="startCopyError" class="rd-copy-error" role="alert">{{startCopyError}}<br><button class="btn sm" @click="startCopyError=''">Dismiss</button></div>
    <span class="rd-sr-only" role="status">{{startCopied ? 'Copied to clipboard' : ''}}</span>
    <div class="layout" :class="{solo:publicCatalog}">
      <main id="maincontent" tabindex="-1" :class="{flush:view==='find'}">
        <!-- The Catalog page has its own, larger search (CatalogPage.vue): there it also finds tools
             for a described job. -->
        <div v-if="!publicCatalog && (view==='tools'||view==='resources')" class="rd-view-search search"><img src="/media/redesign/search.svg" alt=""><input :ref="el => setElement('search', el)" v-model="q" :placeholder="view==='resources'?'Search team resources…':'Search your own tools…'" aria-label="Search"></div>
        <div v-if="err" class="banner">{{err}}</div>
        <div v-if="pendingInvites.length" class="banner" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span>You've been invited:</span>
          <span v-for="inv in pendingInvites" :key="inv.id" style="display:inline-flex;align-items:center;gap:6px">
            <b>{{inv.name}}</b> <span class="role" :class="inv.role">{{inv.role}}</span>
            <button class="btn sm primary" @click="acceptInvite(inv)">Accept</button>
          </span>
        </div>

        <!-- TOOLS -->
        <CatalogPage v-if="view==='connections'" />

        <!-- FIND: /search, a described job answered over the platform pile -->
        <SearchPage v-if="view==='find'" />

        <!-- MARKETPLACE: one integration -->
        <ProviderPage v-if="view==='provider' && mkProvider" />

        <!-- MARKETPLACE: one platform, from the endpoint catalog -->
        <PlatformPage v-if="view==='platform'" />

        <ToolsPage v-if="view==='tools'" />

        <!-- DETAIL (shareable deep links: /app/skills/<name> + /app/tools/<name>) -->
        <DetailPage v-if="view==='detail' && detail" />

        <!-- SECRETS -->
        <SecretsPage v-if="view==='secrets'" />

        <TeamResourcesPage v-if="view==='resources'" />

        <!-- ORGS -->
        <TeamPage v-if="view==='orgs'" />

        <!-- ACTIVITY -->
        <ActivityPage v-if="view==='activity'" />

        <!-- ADMIN -->
        <AdminPage v-if="view==='admin'" />

        <!-- GETTING STARTED -->
        <GettingStartedPage v-if="view==='start'" />

        <!-- REFERRALS — a person's link and everyone who used it. A top-level view (never nested):
             a view inside a view renders nowhere, and the nav button would look dead. -->
        <ReferralsPage v-if="view==='referrals'" />

        <!-- HELP -->
        <HelpPage v-if="view==='help'" />
      </main>
    </div>

    <div v-if="keyMenu" class="key-actions-menu" role="menu" :style="{top:keyMenu.top+'px',right:keyMenu.right+'px'}" @click.stop>
      <button v-if="keyMenu.key.can_rename" role="menuitem" @click="editKey=keyMenu.key.id; editKeyName=keyMenu.key.name; keyMenu=null">Rename</button>
      <button v-if="keyMenu.key.can_disable" role="menuitem" @click="requestKeyAction(keyMenu.key,'disable')">Disable</button>
      <button v-if="keyMenu.key.can_enable" role="menuitem" @click="requestKeyAction(keyMenu.key,'enable')">Enable</button>
      <button v-if="keyMenu.key.can_revoke" class="danger" role="menuitem" @click="requestKeyAction(keyMenu.key,'revoke')">Revoke</button>
      <button v-if="keyMenu.key.can_hide && keyMenu.key.state==='revoked'" role="menuitem" @click="requestKeyAction(keyMenu.key,'hide')">Hide</button>
    </div>

    <div v-if="keyConfirm" class="scrim" role="dialog" aria-modal="true" aria-labelledby="key-confirm-title" @click.self="keyConfirm=null">
      <div class="modal" style="width:min(470px,94vw);padding:18px 20px">
        <h3 id="key-confirm-title" style="margin:0">{{keyConfirm.action==='rotate'?'Rotate':keyConfirm.action==='disable'?'Disable':keyConfirm.action==='revoke'?'Revoke':'Hide'}} “{{keyConfirm.key.name}}”?</h3>
        <p v-if="keyConfirm.action==='rotate'" class="sub" style="margin:12px 0 0"><template v-if="keyConfirm.key.kind==='default_human'">This team's current Getting Started token will stop working immediately. The replacement will be shown next and remain revealable on Getting Started.</template><template v-else>The current key will stop working immediately. The replacement will be shown next so you can update every client using it.</template></p>
        <p v-else-if="keyConfirm.action==='disable'" class="sub" style="margin:12px 0 0">Calls using this key will stop until you enable it again. Its value will not change.</p>
        <p v-else-if="keyConfirm.action==='revoke'" class="sub" style="margin:12px 0 0"><template v-if="keyConfirm.key.kind==='agent'">This removes the agent from the team and revokes all its keys. Historical Activity will remain available.</template><template v-else>This key will stop working permanently. Historical Activity will remain available.</template></p>
        <p v-else class="sub" style="margin:12px 0 0">This revoked key will disappear from the API Keys list. Historical Activity will remain available.</p>
        <div class="row-actions" style="display:flex;justify-content:flex-end;margin-top:20px"><button class="btn sm" @click="keyConfirm=null">Cancel</button><button class="btn sm" :class="{danger:keyConfirm.action!=='hide'}" :disabled="keyBusy" @click="confirmKeyAction">{{keyBusy?'…':'Confirm '+keyConfirm.action}}</button></div>
      </div>
    </div>

    <!-- REGISTER SKILL (bundle) -->
    <!-- Marketplace dialogs. App-ROOT level, like every other dialog: nested inside the
         view==='connections' template they simply did not render on an integration page, so
         Connect looked dead and the modal appeared on the list view once you navigated back. -->
    <ConnectTokenDialog v-if="tokenAsk" />

    <!-- TOP UP. Four presets + Other, the bonus each earns, and auto top-up as a toggle that is
         ON by default for a team with no mandate yet. The toggle IS the consent control: its label
         is the PSD2/SCA mandate text (amount, threshold), and Pay records it through
         /billing/autotopup BEFORE opening Checkout — so the card Checkout saves arms auto top-up
         from the setup webhook with numbers a human agreed to. Off = plain one-off top-up. -->
    <TopUpDialog v-if="topupOpen&&billing" />

    <AgentGuideDialog v-if="capAsk" />

    <ConnectionMethodDialog v-if="methodAsk" />

    <ResourcePickerDialog v-if="resPick" />

    <ExtraCredentialDialog v-if="newSkill" />

    <!-- ADD / EDIT TOOL -->
    <EditToolDialog v-if="newTool" />

    <!-- JOIN BY CODE -->
    <div class="scrim" role="dialog" aria-modal="true" v-if="showJoin" @click.self="showJoin=false">
      <div class="modal" style="width:min(460px,92vw)"><div class="hd"><b>Join with an invite code</b><button class="btn sm" @click="showJoin=false" aria-label="Close">✕</button></div>
        <div style="padding:18px"><p class="sub" style="margin-top:0">Paste the one-time code an admin gave you. It must match your email (<span class="mono">{{me}}</span>). Invites addressed to you also appear automatically as a banner.</p>
          <div class="field"><input v-model="joinCode" placeholder="one-time invite code" @keyup.enter="joinByCode"/></div>
          <button class="btn primary" @click="joinByCode" :disabled="joinBusy">{{joinBusy?'Joining…':'Join'}}</button>
          <div v-if="joinErr" class="banner" style="margin-top:12px">{{joinErr}}</div>
        </div></div>
    </div>

    <!-- INVITED: accept pending invites (multi-select, all checked, the clicked link's team first).
         Opens on first run (no team yet) AND whenever an invite link lands (?invite_org=), even for
         users already in other teams. Decline → create-your-own (first run) or just close. -->
    <AcceptInvitesDialog v-if="inviteChoice && pendingInvites.length" />

    <!-- FIRST-RUN WELCOME: name your team → pick your agent → the setup line (the primary onboarding path) -->
    <WelcomeDialog v-if="welcome.on" />

    <!-- CREATE TEAM -->
    <div class="scrim" role="dialog" aria-modal="true" v-if="newOrg" @click.self="newOrg=false">
      <div class="modal" style="width:min(440px,92vw)"><div class="hd"><b>Create a team</b><button class="btn sm" @click="newOrg=false" aria-label="Close">✕</button></div>
        <div style="padding:18px"><p class="sub" style="margin-top:0">You'll be its owner - invite teammates after.</p>
          <div class="field"><input v-model="newOrgName" placeholder="Team name, e.g. Superdesign" @keyup.enter="createOrg"/></div>
          <button class="btn primary" @click="createOrg" :disabled="orgBusy">{{orgBusy?'Creating…':'Create'}}</button>
          <div v-if="orgErr" class="banner" style="margin-top:12px">{{orgErr}}</div>
        </div></div>
    </div>

    <!-- ADD ORG -->
    <div class="scrim" role="dialog" aria-modal="true" v-if="addOrg" @click.self="addOrg=false">
      <div class="modal" style="width:min(440px,92vw)"><div class="hd"><b>Add an organization</b><button class="btn sm" @click="addOrg=false" aria-label="Close">✕</button></div>
        <div style="padding:18px"><p class="sub" style="margin-top:0">Paste that org's token (each org has its own).</p>
          <div class="field"><input v-model="tokenInput" type="password" placeholder="X-Treg-Token"/></div>
          <button class="btn primary" @click="addToken(tokenInput, true)" :disabled="busy">{{busy?'Checking…':'Add'}}</button>
          <div v-if="loginErr" class="banner" style="margin-top:12px">{{loginErr}}</div>
        </div></div>
    </div>

    <!-- COPY -->
    <CopyToolDialog v-if="copyTool" />

    <!-- INSTALL A RECIPE (recipe-only bundle: how to install/use, no proxy call) -->
    <div class="scrim" role="dialog" aria-modal="true" v-if="copyRecipe" @click.self="copyRecipe=null">
      <div class="modal"><div class="hd"><b>Install “{{copyRecipe.name}}”</b><button class="btn sm ico" @click="copyRecipe=null" aria-label="Close">✕</button></div>
        <div style="padding:16px 18px">
          <div class="tabs"><button v-for="t in ['cURL','CLI','Claude Code']" :key="t" :class="{active:recipeTab===t}" @click="recipeTab=t">{{t}}</button></div>
          <p class="explain">A recipe is know-how (a <span class="mono">SKILL.md</span>), not an API - you don't call it, you <b>install</b> it into <span class="mono">.claude/skills/</span> so an agent can use it.</p>
          <pre class="code" v-html="recipeSnip.html"></pre>
          <div style="margin-top:12px"><button class="btn primary" @click="copy(recipeSnip.text)">⧉ {{copied?'Copied!':'Copy'}}</button></div>
        </div></div>
    </div>

    <!-- AGENT SETUP GUIDE (an instruction to paste into a coding agent) -->
    <div class="scrim" role="dialog" aria-modal="true" v-if="agentGuide" @click.self="agentGuide=null">
      <div class="modal" style="width:min(680px,95vw)"><div class="hd"><b>{{agentGuide==='admin'?'Sync your skills &amp; secrets':'Use your team’s shared tools'}}</b><button class="btn sm ico" @click="agentGuide=null" aria-label="Close">✕</button></div>
        <div style="padding:16px 18px">
          <p class="explain">Paste this into your coding agent (Claude Code / Codex / Gemini). One line — the agent reads llms.txt and does the rest: installs the CLI, signs in as you, and makes its first call. No API keys land on your machine.</p>
          <pre class="code" style="white-space:pre-wrap;max-height:44vh;overflow:auto">{{agentPromptText}}</pre>
          <div style="margin-top:12px;display:flex;gap:10px;align-items:center"><button class="btn primary" @click="copyAgentPrompt">⧉ {{agentGuideCopied?'Copied!':'Copy'}}</button></div>
        </div></div>
    </div>

    <!-- LIST AS VENDOR (an instruction the vendor pastes into THEIR coding agent; it raises the PR) -->
    <ImportSkillDialog v-if="vendorAsk" />

    <!-- REQUEST A TOOL (a "the catalog doesn't have X" report — saved server-side, steers what gets keyed next) -->
    <RequestToolDialog v-if="reqAsk" />

    <!-- SHARE A DETAIL PAGE (invite someone new; they land right here after sign-in) -->
    <ShareDialog v-if="share.on && detail" />

    <!-- VIEW A RECIPE (the SKILL.md content) -->
    <RecipeDialog v-if="viewRecipe" />

    <!-- TRY -->
    <!-- USE drawer: one entry point, two verbs — API call (proxy) / CLI run (server) -->
    <RunToolDialog v-if="tryTool" />

    <!-- Try a MARKETPLACE endpoint right here. Same relay as any call — served by the org's own
         key when one exists, else by a verified public upstream route when declared, else treg's
         key billed to the team balance — and logged in Activity exactly like a CLI call. -->
    <!-- Activity → one call. The archive's copy of what was asked and what came back (metered
         platform calls only — own-key and own-tool calls are relayed without being stored). -->
    <CallDetailsDialog v-if="callView" />
    <TryEndpointDialog v-if="epTry" />

    <!-- Toasts, bottom right: a newer dashboard build is live, and the access reminder (fired when
         a new tool is registered while some members have customized access). -->
    <div v-if="newVersion" class="app-toast" role="status">
      <p>A new version of the dashboard is available.</p>
      <div class="app-toast-a"><button class="btn sm" @click="newVersion=false">Later</button><button class="btn sm primary" @click="reloadApp()">Refresh now</button></div>
    </div>
    <div v-if="accessNote" class="app-toast" role="status">
      <p>{{accessNote}}</p>
      <div class="app-toast-a"><button class="btn sm" @click="accessNote=''">Dismiss</button><button class="btn sm primary" @click="go('org')">Open Team</button></div>
    </div>
  </template>

  <!-- SIGN-IN MODAL — a SIBLING of both branches. It used to live inside the logged-out
       landing, which meant the public catalog (which renders the app shell, not that branch)
       had no way to sign anyone in and every CTA had to navigate away to find one. -->
  <FishVoiceDialog v-if="fishVoiceDialog" />
  <SignInDialog  />
</div>
</div>
</template>

<style scoped>
.boot-status { min-height: 70vh; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px; color: var(--muted); }
.boot-status .brand { color: var(--text); text-decoration: none; }
</style>
