// Google Ads tag — base config and client-side website signup conversion.
//
// Two tracking systems serve different purposes and fire to DIFFERENT action IDs:
//
//   1. CLIENT-SIDE (this file): fires `tregSignupConversion()` once per browser on signup success.
//      This is the **website** conversion for Google Ads action "treg Signup (web)" (7745505287).
//      It populates the Ads UI "SIGNUP" goal under website conversion tracking. The gtag snippet
//      loads from Google and makes a third-party request; a `treg_signup_conv_fired` localStorage
//      flag dedupes so retries or reloads don't double-fire.
//
//   2. SERVER-SIDE (adsconv.py): uploads UPLOAD_CLICKS conversions via the Data Manager API.
//      These go to different action IDs (signup 7723667014, first_call 7723667017, paid 7723667020)
//      and are attributed to the ad click that produced them (gclid/gbraid/wbraid captured in
//      `treg_ad`). The server-side path is durable (outbox → retry → eventual upload) and carries
//      value/currency for the `paid` action.
//
// Both systems coexist: the client event gives Google a real-time website signal for its SIGNUP
// goal; the server upload feeds the bidding model with higher-quality attributed conversions.
// Neither duplicates the other — they track different action IDs entirely.

(function () {
  // Inject Google's gtag.js loader. The script is async; gtag() calls queue until it loads.
  try {
    var GA_MEASUREMENT_ID = 'AW-18392771132';

    window.dataLayer = window.dataLayer || [];
    function gtag() { dataLayer.push(arguments); }
    window.gtag = gtag;
    gtag('js', new Date());
    gtag('config', GA_MEASUREMENT_ID);

    var s = document.createElement('script');
    s.async = true;
    s.src = 'https://www.googletagmanager.com/gtag/js?id=' + GA_MEASUREMENT_ID;
    document.head.appendChild(s);
  } catch (e) { /* never break the page for analytics */ }

  // Expose the signup conversion for the dashboard to call on new-account success.
  // Fires ONCE per browser (localStorage dedupe) so retries/reloads don't double-count.
  window.tregSignupConversion = function () {
    try {
      var KEY = 'treg_signup_conv_fired';
      if (localStorage.getItem(KEY)) return;  // already fired in this browser
      localStorage.setItem(KEY, '1');
      if (typeof gtag !== 'function') return;  // gtag not loaded (should not happen)
      gtag('event', 'conversion', {
        'send_to': 'AW-18392771132/0usqCIeQrO0cELzUrcJE',
        'value': 1.0,
        'currency': 'AUD'
      });
    } catch (e) { /* never throw */ }
  };
})();
