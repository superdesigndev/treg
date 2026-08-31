// First-touch traffic-source capture + product analytics bootstrap. Served by `GET /sitetrack.js`
// with `{POSTHOG_KEY}` / `{POSTHOG_HOST}` substituted server-side (empty key = analytics off) and
// loaded by EVERY public page (landing, use-case pages, resources, the SPA).
//
// Why this exists: the landing page had no analytics at all, so a visitor's first hop (the one
// carrying `utm_*` and the referrer) was invisible — PostHog first saw them on /app after the OAuth
// round-trip, as "$direct". And the signup doors persisted only Google click ids. The result was
// zero source attribution for a campaign we paid for. Two fixes, one script:
//
//   1. A first-party `treg_utm` cookie: first touch wins (never overwritten), 90 days, carrying
//      utm_source|medium|campaign|term|content|referring-host. The signup POST carries it to the
//      server, which persists it on the new team (Org.utm_*). No third-party request, no PII.
//   2. PostHog initialised HERE, on the first page, with pageviews on — so `$initial_utm_*` and
//      `$initial_referring_domain` are stamped on the anonymous session and survive into the
//      identified person after sign-in. index.html defers to this init (window.__phInit).
(function () {
  try {
    var q = new URLSearchParams(window.location.search);
    var has = function (k) { return !!(q.get(k) || '').trim(); };
    var referrer = '';
    try { referrer = document.referrer ? new URL(document.referrer).hostname : ''; } catch (e) {}
    // Our own hosts are not a source: an in-site navigation must not overwrite a real first touch,
    // and must not record "treg.to" as where someone came from.
    if (referrer === window.location.hostname) referrer = '';
    var anyUtm = has('utm_source') || has('utm_medium') || has('utm_campaign');
    var already = /(^|;\s*)treg_utm=/.test(document.cookie);
    if ((anyUtm || referrer) && !already) {
      var pick = function (k) { return (q.get(k) || '').trim().slice(0, 100); };
      var v = encodeURIComponent([pick('utm_source'), pick('utm_medium'), pick('utm_campaign'),
                                  pick('utm_term'), pick('utm_content'), referrer.slice(0, 100)].join('|'));
      // Lax so it survives the cross-site GET from the referring page and the sign-in detour.
      document.cookie = 'treg_utm=' + v + ';path=/;max-age=7776000;samesite=lax' +
        (window.location.protocol === 'https:' ? ';secure' : '');
    }
  } catch (e) { /* never break the page for an attribution cookie */ }

  try {
    var key = '{POSTHOG_KEY}', host = '{POSTHOG_HOST}' || 'https://eu.i.posthog.com';
    if (!key || key.charAt(0) === '{' || window.__phInit) return;
    window.__phInit = true;
    !function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture register register_once register_for_session unregister unregister_for_session getFeatureFlag getFeatureFlagPayload isFeatureEnabled reloadFeatureFlags updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures on onFeatureFlags onSessionId getSurveys getActiveMatchingSurveys renderSurvey canRenderSurvey identify setPersonProperties group resetGroups setPersonPropertiesForFlags resetPersonPropertiesForFlags setGroupPropertiesForFlags resetGroupPropertiesForFlags opt_in_capturing opt_out_capturing has_opted_in_capturing has_opted_out_capturing clear_opt_in_out_capturing debug getPageViewId captureTraceFeedback captureTraceMetric alias set_config startSessionRecording stopSessionRecording sessionRecordingStarted captureException loadToolbar get_property getSessionProperty createPersonProfile get_distinct_id getGroups get_session_id get_session_replay_url".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);
    // identified_only: anonymous visitors cost no person profile, but posthog-js still remembers
    // the first-touch URL/referrer locally and applies it ($set_once) when the person is identified
    // after sign-in. The masking config matches index.html's — the SPA shows API tokens in <pre>
    // blocks and a replay must never leak one.
    window.posthog.init(key, {api_host: host, person_profiles: 'identified_only', capture_pageview: true,
      session_recording: {maskAllInputs: true, maskTextSelector: 'pre, .lc-codewrap, .agent-copy'}});
  } catch (e) { /* analytics must never break the page */ }
})();
