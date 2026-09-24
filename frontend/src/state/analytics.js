
export default {
// Product analytics — only when this deployment opted in (meta.posthog_key present); self-hosters send nothing.
    initAnalytics(){
      // /sitetrack.js (loaded at the bottom of this page, before Vue mounts) normally initialises
      // PostHog already — with pageviews on, so first-touch source survives into the person. Then
      // this only has to identify. The inline path below is the fallback for a stale bundle.
      if(window.__phInit){ this.analyticsIdentify(); return; }
      const key=this.meta && this.meta.posthog_key; if(!key) return; window.__phInit=true;
      const host=this.meta.posthog_host || 'https://eu.i.posthog.com';
      !function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture register register_once register_for_session unregister unregister_for_session getFeatureFlag getFeatureFlagPayload isFeatureEnabled reloadFeatureFlags updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures on onFeatureFlags onSessionId getSurveys getActiveMatchingSurveys renderSurvey canRenderSurvey identify setPersonProperties group resetGroups setPersonPropertiesForFlags resetPersonPropertiesForFlags setGroupPropertiesForFlags resetGroupPropertiesForFlags reset get_distinct_id getGroups get_session_id get_session_replay_url alias set_config startSessionRecording stopSessionRecording sessionRecordingStarted captureException loadToolbar get_property getSessionProperty createPersonProfile opt_in_capturing opt_out_capturing has_opted_in_capturing has_opted_out_capturing clear_opt_in_out_capturing debug getPageViewId captureTraceFeedback captureTraceMetric".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);
      // Session replay is enabled on the project; MASK aggressively — the dashboard shows API tokens
      // and the setup line + curl snippets carry the caller's token in <pre> blocks. Mask all inputs
      // and every code block so a replay can never leak a credential.
      window.posthog.init(key,{api_host:host, person_profiles:'identified_only', capture_pageview:false,
        session_recording:{maskAllInputs:true, maskTextSelector:'pre, .lc-codewrap, .agent-copy'}});
      this.analyticsIdentify();
    },
analyticsIdentify(){ if(!this.me)return; if(window.TregTracking){window.TregTracking.identify(this.me,this.activeSlugNow);return;} if(!window.posthog || !window.posthog.identify || !this.me) return;
      try{ window.posthog.identify(this.me, {email:this.me}); if(this.activeSlugNow) window.posthog.group('team', this.activeSlugNow); }catch(e){} },
track(name, props){ try{ if(window.posthog && window.posthog.capture) window.posthog.capture(name, props||{}); }catch(e){} },
// Support chat (Intercom) — only when this deployment opted in (meta.intercom_app_id present);
    // self-hosters load nothing. Booted AFTER /auth/me resolves (not at the /meta fetch, like
    // analytics) so the common case boots identified once instead of anonymous→identified.
    initIntercom(){
      const app=this.meta && this.meta.intercom_app_id; if(!app || window.__icInit) return; window.__icInit=true;
      window.intercomSettings=this.intercomPayload();
      const s=document.createElement('script'); s.async=true; s.src='https://widget.intercom.io/widget/'+app;
      const i=function(){i.c(arguments)}; i.q=[]; i.c=function(a){i.q.push(a)}; if(!window.Intercom) window.Intercom=i;
      document.head.appendChild(s);
      try{ window.Intercom('boot', this.intercomPayload()); }catch(e){}
    },
intercomPayload(){
      const p={app_id:this.meta.intercom_app_id};
      // Never send email without user_hash: an unhashed identify is exactly the impersonation
      // vector identity verification exists to close. No hash (or no login) = anonymous visitor chat.
      if(this.me && this.icHash){ p.email=this.me; p.user_hash=this.icHash;
        if(this.activeSlugNow) p.company={id:this.activeSlugNow, name:this.activeSlugNow}; }
      return p;
    },
intercomUpdate(){ try{ if(window.__icInit && window.Intercom) window.Intercom('update', this.intercomPayload()); }catch(e){} }
}
