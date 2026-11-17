/* Every "Start free" / "Get started" link to /app on a comparison page: signed in, go to the app;
   signed out, open the sign-in dialog here, without leaving the page. The link's `ref` is kept in
   `treg-ref` as /app?ref= would keep it (frontend boot.js), so the sign-up still names its page. */
(function () {
  var scrim = document.getElementById('signin-scrim');
  if (!scrim) return;
  var next = '/app';
  function refOf(href) { try { return new URL(href, location.href).searchParams.get('ref') || ''; } catch (e) { return ''; } }
  function stash(ref) { if (ref) { try { localStorage.setItem('treg-ref', ref); } catch (e) {} } }
  function track(event, props) { try { if (window.TregTracking) TregTracking.capture(event, props); } catch (e) {} }
  function open() { scrim.classList.add('open'); document.getElementById('em-input').focus(); }
  function close() { scrim.classList.remove('open'); }
  document.getElementById('signinClose').addEventListener('click', close);
  scrim.addEventListener('click', function (e) { if (e.target === scrim) close(); });
  addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
  [].slice.call(document.querySelectorAll('a[href^="/app?ref="]')).forEach(function (a) {
    a.addEventListener('click', function (e) {
      var ref = refOf(a.getAttribute('href'));
      stash(ref);
      track('compare_cta_click', { ref: ref });
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return; /* let new-tab clicks be */
      e.preventDefault();
      next = a.getAttribute('href');
      fetch('/auth/me', { credentials: 'same-origin' }).then(function (r) {
        if (r.ok) location.href = next; else open();
      }).catch(open);
    });
  });
  var btn1 = document.getElementById('em-btn1'), btn2 = document.getElementById('em-btn2');
  var input = document.getElementById('em-input'), code = document.getElementById('em-code'), err = document.getElementById('em-err');
  btn1.addEventListener('click', start);
  input.addEventListener('keyup', function (e) { if (e.key === 'Enter') start(); });
  btn2.addEventListener('click', verify);
  code.addEventListener('keyup', function (e) { if (e.key === 'Enter') verify(); });
  function start() {
    var em = input.value.trim();
    if (!em) { err.textContent = 'Enter your email address.'; return; }
    if (btn1.disabled) return; btn1.disabled = true; btn1.textContent = 'Sending…'; err.textContent = '';
    (window.tregCaptcha ? tregCaptcha.token('email_start') : Promise.resolve(''))
      .then(function (t) { return fetch('/auth/email/start', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ email: em, captcha_token: t }) }); })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) throw new Error(d.detail || 'could not send the code'); return d; }); })
      .then(function (d) {
        document.getElementById('em-step1').style.display = 'none';
        document.getElementById('em-step2').style.display = 'block';
        if (d.dev_code) { code.value = d.dev_code; err.style.color = '#118453'; err.textContent = 'dev mode — your code: ' + d.dev_code; }
        else { err.style.color = ''; }
        code.focus();
      })
      .catch(function (e2) { err.textContent = e2.message; btn1.disabled = false; btn1.textContent = 'Email me a sign-in code'; });
  }
  function verify() {
    if (!code.value.trim()) return;
    btn2.textContent = 'Verifying…'; err.textContent = '';
    fetch('/auth/email/verify', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ email: input.value.trim(), code: code.value.trim() }) })
      .then(function (r) { if (!r.ok) return r.json().catch(function () { return {}; }).then(function (d) { throw new Error(d.detail || "that code didn't work"); }); location.href = next; })
      .catch(function (e2) { err.textContent = e2.message; btn2.textContent = 'Sign in'; });
  }
})();

/* "Copy markdown": the page's .md twin, to the clipboard. */
(function () {
  [].slice.call(document.querySelectorAll('[data-md]')).forEach(function (btn) {
    btn.addEventListener('click', function () {
      fetch(btn.getAttribute('data-md')).then(function (r) { return r.text(); })
        .then(function (t) { return navigator.clipboard.writeText(t); })
        .then(function () { var l = btn.querySelector('.lbl'); l.textContent = 'Copied ✓'; btn.classList.add('ok');
          setTimeout(function () { l.textContent = 'Copy markdown'; btn.classList.remove('ok'); }, 1800); })
        .catch(function () { location.href = btn.getAttribute('data-md'); });
    });
  });
})();
