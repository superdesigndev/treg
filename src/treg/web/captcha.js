// Cloudflare Turnstile for every email sign-in form: `await tregCaptcha.token('email_start')`
// resolves to a fresh single-use token, '' when this deployment has no widget, or `!<reason>`
// when the browser could not get one (`!blocked` the script did not load, `!pending` still
// waiting for a click, `!error` the widget failed). The server never sends a `!` value to
// Cloudflare; it only picks the message from it (infra/turnstile.py `client_reason`). Served by
// routers/web.py, which fills in the site key; an empty key turns every call into ''.
//
// Only fields marked `data-captcha="email_start"` start a check ahead of time, on focus, so the
// click rarely waits; other email fields (invites, sharing) never summon a widget.
(function () {
  var SITE_KEY = "__TREG_TURNSTILE_SITE_KEY__";
  var API = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
  var WAIT_MS = 30000;       // how long a submit waits for an unanswered check before sending without
  var LOAD_WAIT_MS = 10000;  // a script still missing by then is treated as blocked, not as pending
  var FRESH_MS = 240000;     // a token lives 300s at Cloudflare; spend a prefetched one well before
  var loading = null, field = null;
  // The one check that matters: running, or holding an unclaimed token. A claimed check belongs
  // to the submit waiting on it; a new check replaces `current` only when that one is unusable.
  var current = null;

  function load() {
    if (window.turnstile) return Promise.resolve(window.turnstile);
    loading = loading || new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = API; s.async = true;
      s.onload = function () { resolve(window.turnstile); };
      s.onerror = function () { loading = null; s.remove(); reject(new Error("turnstile unavailable")); };
      document.head.appendChild(s);
    });
    return loading;
  }

  // Invisible unless Cloudflare asks for a click; then it floats just under the form being
  // filled in. Inside an open modal <dialog> it must live in the dialog: the top layer sits above
  // any z-index and the rest of the page is inert there.
  function host() {
    var dialog = field && field.isConnected && field.closest("dialog[open]");
    return dialog || document.body;
  }

  function place(el) {
    var around = field && field.isConnected
      && (field.closest("[role=dialog], dialog") || field.closest("form") || field.parentElement);
    var r = around && around.getBoundingClientRect();
    var view = window.visualViewport;
    var bottom = view ? view.offsetTop + view.height : window.innerHeight;
    el.style.cssText = "position:fixed;transform:translateX(-50%);z-index:2147483000;" + (r && r.height
      ? "top:" + Math.max(8, Math.min(r.bottom + 12, bottom - 80)) + "px;left:" + (r.left + r.width / 2) + "px"
      : "top:" + Math.max(8, bottom - 96) + "px;left:50%");
  }

  function theme() {
    var t = document.documentElement && document.documentElement.getAttribute("data-theme");
    return t === "dark" || t === "light" ? t : "auto";
  }

  // One widget, alive until Cloudflare answers or the check is cancelled. A check waiting for a
  // click is never torn down by a timer, however long the person takes to notice it.
  function start(action) {
    // Until the widget exists, cancelling just marks the check so it never renders.
    var check = { action: action, at: Date.now(), done: false, failed: false, claimed: false,
                  cancel: function () { check.failed = true; } };
    check.promise = load().then(function (turnstile) {
      if (check.failed) return "!error";  // cancelled or given up while the script loaded
      return new Promise(function (resolve) {
        var el = document.createElement("div"), id = null, view = window.visualViewport;
        el.setAttribute("data-treg-captcha-widget", "");
        // Shown only when Cloudflare wants a click: a bare checkbox under a dialog is easy to miss.
        var note = document.createElement("div"), slot = document.createElement("div");
        note.textContent = "One quick check before we email your code";
        var dark = theme() === "dark";
        note.style.cssText = "display:none;margin:0 auto 6px;width:max-content;max-width:300px;padding:5px 10px;"
          + "border-radius:8px;font:13px/1.4 system-ui,-apple-system,sans-serif;text-align:center;"
          + "box-shadow:0 1px 4px rgba(0,0,0,.18);" + (dark ? "background:#262626;color:#f2f2f2" : "background:#fff;color:#1d1d1f");
        el.appendChild(note); el.appendChild(slot);
        place(el);
        host().appendChild(el);
        function follow() { place(el); }
        function visible() { note.style.display = "block"; follow(); }
        if (view) { view.addEventListener("resize", follow); view.addEventListener("scroll", follow); }
        function finish(value) {
          if (check.done) return;
          check.done = true;
          if (view) { view.removeEventListener("resize", follow); view.removeEventListener("scroll", follow); }
          try { if (id !== null) turnstile.remove(id); } catch (e) { /* already gone */ }
          el.remove(); resolve(value);
        }
        check.cancel = function () { finish("!error"); };
        try {
          id = turnstile.render(slot, {
            sitekey: SITE_KEY, action: action, appearance: "interaction-only", theme: theme(),
            callback: function (t) { finish(t || "!error"); },
            "before-interactive-callback": visible,  // about to need a click: explain it, measure now
            // The code names the cause (e.g. 110200 = this hostname is not on the widget).
            "error-callback": function (code) { if (window.console) console.warn("turnstile error", code); finish("!error"); return true; },
            "timeout-callback": function () { finish("!error"); },
          });
        } catch (e) { finish("!error"); }
      });
    }, function () { return "!blocked"; }).catch(function () { return "!error"; }).then(function (t) {
      check.done = true;
      if (t.charAt(0) === "!") check.failed = true;
      return t;
    });
    return check;
  }

  function usable(check, action) {
    return !!check && check.action === action && !check.failed && !check.claimed && Date.now() - check.at < FRESH_MS;
  }

  // Start a check ahead of time unless one is already running or holding a token: refocusing
  // the field never floats a second widget.
  function prefetch(action) {
    if (current && current.action === action && !current.done && Date.now() - current.at < FRESH_MS) return;
    if (usable(current, action)) return;
    if (current && !current.done) current.cancel();
    current = start(action);
  }

  // Each token is single-use: the check is claimed here, so a second caller (a double click)
  // starts its own instead of resending the same token. Waiting is bounded, the check is not:
  // after WAIT_MS the request goes out with `!pending` and the check is released, still on screen,
  // so the next click finds its token. A script still missing after LOAD_WAIT_MS is `!blocked`.
  function token(action) {
    if (!SITE_KEY) return Promise.resolve("");
    var check = usable(current, action) ? current : null;
    if (!check) {
      if (current && !current.done && !current.claimed) current.cancel();
      check = current = start(action);
    }
    check.claimed = true;
    return new Promise(function (resolve) {
      var settled = false;
      function give(value) { if (!settled) { settled = true; clearTimeout(slow); clearTimeout(timer); resolve(value); } }
      var slow = setTimeout(function () {
        if (window.turnstile || check.done) return;
        loading = null;  // let the next click try the script again
        check.cancel();
        give("!blocked");
      }, LOAD_WAIT_MS);
      var timer = setTimeout(function () {
        // Released: the widget stays up and the next click takes its token. A check that is no
        // longer `current` (a second submit replaced it) has nobody left to collect it.
        if (current === check) check.claimed = false;
        else check.cancel();
        give("!pending");
      }, WAIT_MS);
      check.promise.then(function (t) { give(t); });
    });
  }

  if (SITE_KEY) {
    // Warm the script so the click only waits for the check itself, not the download.
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { load().catch(function () {}); });
    else load().catch(function () {});
    document.addEventListener("focusin", function (e) {
      var el = e.target, action = el && el.getAttribute && el.getAttribute("data-captcha");
      if (!action) return;
      field = el;
      prefetch(action);
    });
  }
  window.tregCaptcha = { token: token, enabled: !!SITE_KEY };
})();
