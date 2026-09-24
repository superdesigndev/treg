import { createElements } from './context'
import { LS } from './constants.js'
import { FIND_EMPTY } from './find.js'
export default function data(){
    let cfg={active:null,orgs:{}}; try{ cfg=JSON.parse(localStorage.getItem(LS))||cfg; }catch(e){}
    return {
      elements: createElements(),
      bootReady: false, bootFailed: false,
      theme: localStorage.getItem('treg-theme')||'light',
      mobileNav: false,  // mobile sidebar toggle
      // True when this load is a PUBLIC catalog URL (/catalog, /catalog/<slug>). The catalog API is
      // unauthenticated, so the same marketplace views render for a signed-out visitor — that is
      // what makes the catalog indexable without maintaining a second copy of the UI. Everything
      // that needs a session (org switcher, vault, activity, team, try-it) is hidden on `authed`.
      publicCatalog:false, oauthSignin:false,
      cfg, tokenInput:'', busy:false, loginErr:'', addOrg:false, orgMenu:false, orgMenuStyle:null,
      emailInput:'', codeInput:'', emailStage:false, devCode:'', pendingInvites:[], invitePrefill:'', inviteChoice:false, inviteBusy:false,
      inviteSel:{}, inviteLinkOrg:null, inviteErr:'',  // multi-select accept: checked ids, the org_id the clicked email link was for, partial-failure note
      tut:{i:0, panel:null}, tutCopied:false, tourI:0, helpMode:null, xtut:{i:0},
      newOrg:false, newOrgName:'', orgBusy:false, orgErr:'', orgMsg:'',
      orgMembers:[], orgInvites:[], inviteEmail:'', inviteRole:'member', lastInvite:null,
      editAccess:null, accessDraft:{}, inviteCustomize:false, inviteLocalRun:true, inviteToolSel:{}, accessNote:'',
      // agents (machine identities), projects (sub-scope) and deny rules (policy)
      orgTab:'members', showInvite:false, showAddAgent:false,
      apiKeys:[], keyName:'', keyNameInvalid:false, keyBusy:false, keyErr:'', keyMsg:null, newApiKey:null, editKey:null, editKeyName:'', keyMenu:null, keyConfirm:null, activityKey:'',
      agentSnip:'prompt',   // which paste-ready snippet the agent card shows (prompt = hand-to-agent, first)
      snipAgent:null,    // an EXISTING agent whose setup snippets are open (no token — placeholder)
      agents:[], agentName:'', agentRole:'member', agentCap:-1, agentBusy:false, agentErr:'', agentProjSel:{}, agentAccessMode:null, agentToolSel:{},
      observedAgents:[], promoteHint:'', promotePending:null, agentTokens:{},  // minted tokens, THIS page-load only — the server stores hashes
      newAgent:null, confirmAgent:null,
      projects:[], projectName:'', projBusy:false, confirmProj:null, projDraft:{},
      editProj:null, projToolDraft:{}, projToolBusy:false,
      denyRules:[], denyForm:{host:'',path_prefix:'',method:'',user_id:null,project_id:null,note:''}, denyBusy:false, confirmDeny:null, cliDeny:[],
      bootVersion:'', newVersion:false,
      usage:null, usageDays:30, myUsage:null, actTab:'feed', buildTab:'vendor',  // Activity page: 'feed' | 'usage'
      tagUsage:{}, tagKeys:[],   // spend per X-Treg-Meta key: {customer:{…}, workspace:{…}}
  // usage-metering: rollups + the member's own used/cap
      // Billing (Stripe top-ups). `billing` null = not loaded / not an admin; billing.configured
      // false = this deployment sells no balance, so the whole block stays hidden.
      billing:null, billingBusy:false, topupAmount:10, autoAmount:10, autoThreshold:5, autoConsent:false, autoOpen:false,
      topupOpen:false, topupPick:10, topupOther:null, topupAuto:true, topupErr:'',
      capCfg:null, capUsd:0, capBusy:false, capErr:'',
      renameName:'', renameSlug:'', renameBusy:false, renameErr:'',
      budgets:[], budDims:[], budDim:'', budVal:'', budDaily:'', budBusy:false, budErr:'',
      bhist:{items:[],loading:false,ok:true},   // past top-ups + their invoice/receipt links; ok=false means Stripe was unreachable, amounts are still right
      // Referral program. Seeded with the same SHAPE the API returns (terms/totals/cap present and
      // zeroed) so the template can read ref.terms.hold_days on the very first paint — a v-if on
      // `loading` guards the table, but the subtitle above it renders immediately.
      refTab:'friend',  // 'friend' | 'partner' — the fork at the top of the Referrals view
      ref:{loading:false,eligible:false,code:'',link:'',credit_org:null,referrals:[],
           terms:{referrer_micro:0,referred_micro:0,min_topup_micro:0,hold_days:0},
           totals:{signed_up:0,topped_up:0,earned_micro:0,pending_micro:0},cap:{paid:0,limit:0}},
      refCopied:false,
      confirmDel:'', confirmLeave:false, confirmRemove:null,
      showJoin:false, joinCode:'', joinBusy:false, joinErr:'',
      secrets:[], showSecrets:false, secretRows:[{name:'',value:'',kind:'env'}], secretBusy:false, secretErr:'', runNote:'',
      newTool:false, toolBusy:false, toolErr:'', confirmDelTool:null, confirmDelSecret:null,
      tForm:{id:null, name:'', base_url:'', bindings:[]},
      addToolMenu:false,  // the ＋ Add tool [endpoint | cli] chooser
      // Connections: registry OAuth connects (see oauth_providers.py). `providers` is what treg
      // holds an approved app for; `connections` is what this org has actually connected.
      providers:[], connections:[], connErr:'', connBusy:false, confirmDisc:null, resPick:null,
      mkCat:'', mkService:null,  // marketplace: active category filter, and the open integration
      byokFocus:null,  // provider row to flash after a "Bring your own key" jump to the Platform tab
      // Marketplace tab bar: 'all' + one key per catalog category, plus 'platform' for the
      // original integration shelves. Data-first is the default view.
      mkTab:'all',
      platLogoBad:{},  // platform slug → we have no /logos/platforms/<slug>.svg, so draw the initial tile
      // Endpoint catalog (GET /catalog/*): the platform axis of the marketplace. Everything here is
      // optional — a server without the catalog routes just renders no platform shelf.
      plats:{list:[], loaded:false, loading:false},
      // Find tools for a job (state/find.js): phase idle | recall | reading | done | error
      find:{...FIND_EMPTY}, findCopied:'',
      platSlug:null, platData:null, platErr:'', platLoading:false,
      platShelfOpen:{},    // category → its featured shelf has been expanded to the full tile list
      // The ledger's filter bar. All three narrow the SAME row list, and a section with no
      // surviving rows disappears rather than showing an empty heading.
      platDomain:'', platQ:'', platVerifiedOnly:false,
      platOpen:{},         // ledger row key → row expanded
      platActionsOpen:false,  // the single platform-wide account/utility ("Actions") section is open
      epOpen:{},           // endpoint id → its provider sub-row (level two, merged rows only) is open
      epTab:{},            // endpoint id → which pane of its detail is showing ('req' | 'res')
      platEx:{},           // endpoint id → {open, loading, err, text} for the lazily-fetched example
      platCopied:'',       // endpoint id whose `treg call` line was just copied
      capAsk:null,  // the access question, asked when a one-method provider has several scope levels
      methodAsk:null,  // separate grants behind one provider: one selected method, then Continue
      tokenAsk:null,  // bring-your-own-bot setup (Slack): a form, not a redirect
      extraCred:{}, extraBusy:null,  // second credential a provider needs on top of OAuth (Google Ads' developer token)
      cfgMenu:false,  // detail-page ⚙ Configure tool-picker (skills with >1 tool)
      tryMenu:false,  // detail-page ▶ Try it tool-picker (skills with >1 tool)
      // marketplace endpoint Try-it drawer
      callView:null, callViewFull:false, callCopied:'',  // Activity → one call's request/response drawer
      actOkOnly:true,  // Activity feed: successes only by default; the toggle shows failed/refused too
      epTry:null, epTryTab:'agent', epTryAccess:null, epTryAccessByMethod:{}, epTryAuthMethod:'', epTryParams:[], epTryBody:'', epTryResp:'', epTryStatus:null,
      epTryMs:null, epTryCost:null, epTryBusy:false,
      catalogClis:null,  // bin → catalog default deny patterns (lazy, from /providers.json)
      newSkill:false, skillJson:'', skillBusy:false, skillErr:'', skillMode:'folder',
      skillFiles:[], detected:null, skillSel:{}, skillVals:{}, skillResults:null,
      sessionMode:false, activeSlug: localStorage.getItem('treg-active')||null, meta:{github:false, public_url:location.origin},
      view:'tools', q:'', err:'', loading:false, toolTab:'all', bundles:[], confirmDelBundle:null, installCopied:null,
      copyRecipe:null, recipeTab:'cURL', viewRecipe:null, recipeSaved:false,
      // detail pages (shareable /app/skills/<name> + /app/tools/<name> deep links)
      detail:null, detailData:null, detailErr:'', detailLoading:false, detailFile:'SKILL.md', detailCopied:'', detailNote:'',
      shareGate:null,  // logged-out arrival at a shared detail link: {kind,name} → focused sign-in page (no sandbox/tour)
      share:{on:false, email:'', role:'viewer', full:true, busy:false, err:'', sent:null, member:null},  // detail-page "Share…" (invite + land on this page)
      me:'', icHash:'', myOrgs:[], isAdmin:false,
      onboarded:true,  // first-run onboarding done (server flag; gates the welcome modal)
      welcome:{on:false, step:0, name:'', agent:'openclaw', moreOpen:false, busy:false, err:''},  // first-run: name your team → pick your agent → setup line
      emptyTab:'agent',
      tools:[], health:{}, calls:[], runs:[], adminStats:null, adminOrgs:[], adminUsers:[],
      adminBusy:false, confirmAdmUser:null, confirmAdmOrg:null,
      proxy: location.origin, copyTool:null, snippetTab:'cURL', snippetTabs:['cURL','CLI','Claude Code','Python','Node'], copied:false,
      exPath:'<PATH>', exMethod:'GET',
      tryTool:null, tryMethod:'GET', tryPath:'', tryBody:'', trying:false, tryResp:null, tryStatus:0, tryMs:0,
      useMode:'call', runArgsStr:'', running:false, runOut:null,
      startCopyError:'', startCopied:'', startTab:'access', startAgentOpen:false, startTokenShow:false, mdMenu:false, llmsCopied:false, myToken:null, _myTokenOrg:null, defaultKeyId:null, defaultKeyState:null, tokenCopied:false,
      agentGuide:null, agentGuideCopied:false, agentRowCopied:null,
      vendorAsk:false, vendorCopied:false,
      reqAsk:false, reqDone:false, reqBusy:false, reqErr:'', reqForm:{capability:'', note:'', contact:''},
      demo:{signin:false},
    teamResources:[],
teamResourceBusy:false,
teamResourceErr:'',
teamResourceProvider:'',
teamResourceKind:'',
teamResourcePage:1,
teamResourcePageSize:10,
teamResourceCopied:null,
epTryBodyType:'json',
epTryMultipart:[],
epTryFiles:{},
epTryAudioUrl:'',
epTryAudioName:'speech.mp3',
fishVoices:[],
fishVoicesSource:'platform',
fishVoiceBusy:false,
fishVoiceNote:'',
fishVoiceDialog:null,
};
  }
