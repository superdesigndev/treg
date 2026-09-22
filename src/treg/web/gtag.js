// Google Ads measurement, Consent Mode v2 in ADVANCED form. Every page that carries this file
// loads Google's tag with every consent signal DENIED before it initialises: Google sets no
// advertising cookie and reads none, but the tag still sends cookieless pings, which is what lets
// Google MODEL conversions for visitors who never granted anything — the mobile pre-roll viewer
// who signs up later on a laptop. A tag that is not loaded at all sends nothing and models nothing.
//
// The signed-in dashboard includes this file with `data-conversion-only`: no page view is sent
// from there, only the `treg Signup (web)` conversion after the first team is created. That
// action is SECONDARY in the Ads account (excluded from the Conversions column), so it can never
// double count against the server-side upload in adsconv.py, which remains the bidding signal.
//
// A future consent UI grants cookies by calling window.tregAdsConsent('granted'); nothing here
// needs to change for that, and nothing here depends on it.
//
// Conversion ID: AW-18392771132. See docs/context/architecture/ads-conversions.md.
(function () {
  try {
    var TAG_ID = 'AW-18392771132';
    var SIGNUP_DESTINATION = TAG_ID + '/0usqCIeQrO0cELzUrcJE';
    var me = document.currentScript;
    var conversionOnly = !!(me && me.hasAttribute('data-conversion-only'));

    window.dataLayer = window.dataLayer || [];
    function gtag() { window.dataLayer.push(arguments); }
    window.gtag = window.gtag || gtag;

    // Order matters: consent defaults MUST be queued before the tag script is appended and before
    // any config/event, or Google treats the first hits as consented.
    window.gtag('consent', 'default', {
      ad_storage: 'denied',
      ad_user_data: 'denied',
      ad_personalization: 'denied',
      analytics_storage: 'denied'
    });
    // With storage denied, carry the click id through internal links in the URL instead of a
    // cookie, and strip ad-click identifiers from the pings Google does receive.
    window.gtag('set', 'url_passthrough', true);
    window.gtag('set', 'ads_data_redaction', true);

    var s = document.createElement('script');
    s.async = true;
    s.src = 'https://www.googletagmanager.com/gtag/js?id=' + TAG_ID;
    document.head.appendChild(s);

    window.gtag('js', new Date());
    window.gtag('config', TAG_ID, conversionOnly ? {send_page_view: false} : {});

    var fired = {};
    // Called by the dashboard once, right after POST /orgs succeeds for a new user's first team.
    // transaction_id is stable per team so a retry or a reload cannot count the same team twice
    // inside the web action; the server-side outbox has its own (org, action) uniqueness.
    window.tregSignupConversion = function (orgId) {
      try {
        var tid = 'treg-web-signup-' + String(orgId || '');
        if (!orgId || fired[tid]) return;
        fired[tid] = true;
        window.gtag('event', 'conversion', {
          send_to: SIGNUP_DESTINATION, transaction_id: tid, value: 1.0, currency: 'AUD'
        });
      } catch (e) { /* never break the dashboard for a marketing ping */ }
    };

    window.tregAdsConsent = function (state) {
      var granted = state === 'granted';
      window.gtag('consent', 'update', {
        ad_storage: granted ? 'granted' : 'denied',
        ad_user_data: granted ? 'granted' : 'denied',
        ad_personalization: 'denied'
      });
    };
  } catch (e) { /* never break the page for a marketing script */ }
})();
