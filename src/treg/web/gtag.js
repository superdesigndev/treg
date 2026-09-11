// Google Ads measurement. Marketing pages load the base tag; the signed-in dashboard includes
// this file in `data-conversion-only` mode, which defines tregSignupConversion() without contacting
// Google until optional advertising cookies are accepted and the first team is created successfully.
//
// Consent defaults are deliberately queued before config/event. Without an affirmative choice the
// Google is not contacted at all. The dashboard sends no ordinary page view in either state.
// Conversion ID: AW-18392771132
// Signup action: AW-18392771132/0usqCIeQrO0cELzUrcJE
(function () {
  'use strict';

  var TAG_ID = 'AW-18392771132';
  var SIGNUP_DESTINATION = TAG_ID + '/0usqCIeQrO0cELzUrcJE';
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
    return !!(window.tregCookieConsent && window.tregCookieConsent.state() === 'granted');
  }

  function grantMeasurementConsent() {
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

  window.tregSignupConversion = function (transactionId) {
    // The caller invokes this only after POST /orgs succeeds. Consent is site-wide, not a signup
    // checkbox, and Google deduplicates retries of the same transaction ID.
    if (!transactionId || !consentGranted()) return false;
    loadTag(false);
    window.gtag('event', 'conversion', {
      send_to: SIGNUP_DESTINATION,
      transaction_id: String(transactionId),
      value: 1.0,
      currency: 'AUD'
    });
    return true;
  };

  // Basic Consent Mode: before acceptance, neither marketing pages nor the dashboard contact
  // Google. After acceptance, marketing pages load the base tag; the dashboard remains lazy until
  // its one first-team conversion is ready.
  if (!conversionOnly && consentGranted()) loadTag(true);
  if (window.tregCookieConsent) {
    window.tregCookieConsent.onChange(function (state) {
      if (state === 'granted') {
        grantMeasurementConsent();
        if (!conversionOnly) loadTag(true);
      } else {
        window.gtag('consent', 'update', {
          ad_storage: 'denied',
          ad_user_data: 'denied',
          ad_personalization: 'denied',
          analytics_storage: 'denied'
        });
      }
    });
  }
})();
