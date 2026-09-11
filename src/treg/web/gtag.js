// Google Ads measurement. Marketing pages load the base tag; the signed-in dashboard includes
// this file in `data-conversion-only` mode, which defines tregSignupConversion() without contacting
// Google until a person explicitly opts in and their first team has been created successfully.
//
// Consent defaults are deliberately queued before config/event. Without an affirmative choice the
// marketing tag can send only consent-mode cookieless pings and the dashboard sends nothing.
// Conversion ID: AW-18392771132
// Signup action: AW-18392771132/0usqCIeQrO0cELzUrcJE
(function () {
  'use strict';

  var TAG_ID = 'AW-18392771132';
  var SIGNUP_DESTINATION = TAG_ID + '/0usqCIeQrO0cELzUrcJE';
  var CONSENT_KEY = 'treg-google-ads-consent';
  var source = document.currentScript;
  var conversionOnly = !!(source && source.hasAttribute('data-conversion-only'));
  var loaded = false;
  var configured = false;

  window.dataLayer = window.dataLayer || [];
  function gtag() { window.dataLayer.push(arguments); }
  window.gtag = window.gtag || gtag;

  // Google requires these defaults before any config or event command. Personalised advertising
  // and analytics stay off even when conversion measurement is accepted.
  window.gtag('consent', 'default', {
    ad_storage: 'denied',
    ad_user_data: 'denied',
    ad_personalization: 'denied',
    analytics_storage: 'denied'
  });

  function consentGranted() {
    try { return localStorage.getItem(CONSENT_KEY) === 'granted'; }
    catch (e) { return false; }
  }

  function grantMeasurementConsent() {
    try { localStorage.setItem(CONSENT_KEY, 'granted'); }
    catch (e) { /* the current event can still carry the explicit choice */ }
    window.gtag('consent', 'update', {
      ad_storage: 'granted',
      ad_user_data: 'granted',
      ad_personalization: 'denied',
      analytics_storage: 'denied'
    });
  }

  function loadTag(sendPageView) {
    if (!loaded) {
      loaded = true;
      var script = document.createElement('script');
      script.async = true;
      script.src = 'https://www.googletagmanager.com/gtag/js?id=' + TAG_ID;
      document.head.appendChild(script);
    }
    if (!configured) {
      configured = true;
      window.gtag('js', new Date());
      window.gtag('config', TAG_ID, {send_page_view: !!sendPageView});
    }
  }

  if (consentGranted()) grantMeasurementConsent();

  window.tregSignupConversion = function (transactionId, allowed) {
    // The caller supplies `allowed` from the unchecked-by-default first-team consent control and
    // calls only after POST /orgs succeeds. Google deduplicates retries of the same transaction ID.
    if (allowed !== true || !transactionId) return false;
    grantMeasurementConsent();
    loadTag(false);
    window.gtag('event', 'conversion', {
      send_to: SIGNUP_DESTINATION,
      transaction_id: String(transactionId),
      value: 1.0,
      currency: 'AUD'
    });
    return true;
  };

  // Marketing pages retain their base measurement. In conversion-only mode the external script is
  // lazy: an ordinary dashboard view makes no Google request.
  if (!conversionOnly) loadTag(true);
})();
