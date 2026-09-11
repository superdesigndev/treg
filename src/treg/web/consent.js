// Site-wide choice for optional Google Ads measurement.
//
// This file is first-party and makes no network request. Until a visitor accepts, adtrack.js does
// not set treg_ad and gtag.js does not load Google's script. Necessary session/auth cookies are not
// controlled here.
(function () {
  'use strict';

  var STORAGE_KEY = 'treg-cookie-consent-v1';
  var current = readChoice();
  var listeners = [];
  var banner = null;

  function readChoice() {
    try {
      var saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      return saved && saved.version === 1 && (saved.ads === 'granted' || saved.ads === 'denied')
        ? saved.ads : 'unset';
    } catch (e) { return 'unset'; }
  }

  function forgetCookie(name) {
    document.cookie = name + '=;path=/;max-age=0;samesite=lax' +
      (window.location.protocol === 'https:' ? ';secure' : '');
  }

  function clearAdvertisingCookies() {
    forgetCookie('treg_ad');
    try {
      document.cookie.split(';').forEach(function (part) {
        var name = part.split('=')[0].trim();
        if (name.indexOf('_gcl_') === 0) forgetCookie(name);
      });
    } catch (e) { /* cookie access can be disabled */ }
  }

  function notify() {
    listeners.slice().forEach(function (listener) {
      try { listener(current); } catch (e) { /* one listener must not break another */ }
    });
    try {
      window.dispatchEvent(new CustomEvent('treg:ads-consent', {detail: {state: current}}));
    } catch (e) { /* CustomEvent is absent only in very old browsers */ }
  }

  function hide() {
    if (banner && banner.parentNode) banner.parentNode.removeChild(banner);
    banner = null;
  }

  function choose(choice) {
    if (choice !== 'granted' && choice !== 'denied') return;
    current = choice;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        version: 1,
        ads: choice,
        updated_at: new Date().toISOString()
      }));
    } catch (e) { /* keep the choice for this page even if storage is unavailable */ }
    if (choice === 'denied') clearAdvertisingCookies();
    hide();
    notify();
  }

  function show() {
    if (banner || !document.body) return;
    banner = document.createElement('section');
    banner.id = 'treg-cookie-choices';
    banner.setAttribute('role', 'dialog');
    banner.setAttribute('aria-modal', 'false');
    banner.setAttribute('aria-labelledby', 'treg-cookie-title');
    banner.innerHTML =
      '<div class="treg-cookie-copy">' +
        '<strong id="treg-cookie-title">Cookie choices</strong>' +
        '<p>We use optional Google Ads cookies to understand which ads lead to signups. ' +
        'We don\'t use them for analytics or personalized advertising. ' +
        '<a href="/privacy#s7">Learn more</a></p>' +
      '</div>' +
      '<div class="treg-cookie-actions">' +
        '<button type="button" data-choice="denied">Reject</button>' +
        '<button type="button" class="treg-cookie-accept" data-choice="granted">Accept</button>' +
      '</div>';
    banner.style.cssText =
      'position:fixed;z-index:2147483647;left:16px;right:16px;bottom:16px;max-width:920px;' +
      'margin:auto;padding:18px;border:1px solid #d8cdb9;border-radius:12px;background:#fffdf8;' +
      'color:#201c15;box-shadow:0 12px 40px rgba(32,28,21,.22);font:14px/1.45 ' +
      '-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;' +
      'display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap';
    var style = document.createElement('style');
    style.textContent =
      '#treg-cookie-choices .treg-cookie-copy{flex:1 1 420px}' +
      '#treg-cookie-choices strong{display:block;font-size:16px;margin-bottom:4px}' +
      '#treg-cookie-choices p{margin:0;color:#201c15}' +
      '#treg-cookie-choices a{color:#201c15;text-decoration:underline}' +
      '#treg-cookie-choices .treg-cookie-actions{display:flex;gap:8px;flex:1 1 360px}' +
      '#treg-cookie-choices button{flex:1;min-height:42px;padding:8px 12px;border:1px solid #685e4d;' +
        'border-radius:8px;background:#fffdf8;color:#201c15;font:600 13px/1.2 ui-monospace,' +
        '"SFMono-Regular",Consolas,monospace;cursor:pointer}' +
      '#treg-cookie-choices button:hover{background:#f0eadc}' +
      '#treg-cookie-choices button.treg-cookie-accept{background:#201c15;color:#fffdf8}' +
      '#treg-cookie-choices button.treg-cookie-accept:hover{background:#000}' +
      '#treg-cookie-choices button:focus-visible{outline:3px solid #b8461f;outline-offset:2px}' +
      '@media(max-width:600px){#treg-cookie-choices{left:10px!important;right:10px!important;' +
        'bottom:10px!important;padding:15px!important}#treg-cookie-choices .treg-cookie-actions{' +
        'flex-direction:column}#treg-cookie-choices button{width:100%}}';
    banner.appendChild(style);
    banner.addEventListener('click', function (event) {
      var button = event.target.closest && event.target.closest('button[data-choice]');
      if (button) choose(button.getAttribute('data-choice'));
    });
    document.body.appendChild(banner);
  }

  window.tregCookieConsent = {
    state: function () { return current; },
    choose: choose,
    show: show,
    onChange: function (listener) {
      if (typeof listener === 'function') listeners.push(listener);
    }
  };

  // A missing choice is a denial, including for advertising cookies left by an older deployment.
  if (current !== 'granted') clearAdvertisingCookies();
  if (current === 'unset') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', show);
    else show();
  }
})();
