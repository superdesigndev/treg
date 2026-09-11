(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  let toastTimer;
  function toast(message) {
    $('toast').textContent = message;
    $('toast').classList.add('visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $('toast').classList.remove('visible'), 2600);
  }
  async function copy(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
      else {
        const area = document.createElement('textarea');
        area.value = text;
        area.style.cssText = 'position:fixed;opacity:0';
        document.body.append(area);
        area.select();
        const ok = document.execCommand('copy');
        area.remove();
        if (!ok) throw new Error('Clipboard unavailable');
      }
      toast('Copied. Paste it into Codex.');
    } catch (_) { toast('Could not copy. Select the prompt and copy it manually.'); }
  }
  function capture(event, properties = {}) {
    try { window.posthog?.capture(event, { campaign: 'gpt6', ...properties }); } catch (_) { /* Optional analytics. */ }
  }

  // Native dialogs provide focus containment, Escape and focus restoration.
  for (const dialog of document.querySelectorAll('dialog')) {
    dialog.querySelector('.dialog-close').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
  }
  document.querySelectorAll('[data-watch]').forEach((button) => button.addEventListener('click', () => {
    const video = $('launch-film');
    if (!video.getAttribute('src')) video.src = 'media/astra/launch.mp4';
    $('film-dialog').showModal();
    video.play().catch(() => { /* Native controls remain available if autoplay is blocked. */ });
    capture('gpt6_film_play');
  }));
  $('film-dialog').addEventListener('close', () => $('launch-film').pause());
  document.querySelectorAll('[data-setup]').forEach((link) => link.addEventListener('click', async (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const response = await fetch('/auth/me', { credentials: 'same-origin' });
      if (response.ok) { window.location.href = '/app?ref=gpt6'; return; }
    } catch (_) { /* A connection failure must not hide the sign-in options. */ }
    finally { button.disabled = false; }
    try { localStorage.setItem('treg-ref', 'gpt6'); } catch (_) { /* Storage is optional. */ }
    $('account-dialog').showModal();
    ($('email-verify-form').hidden ? $('signin-email') : $('signin-code')).focus();
    syncDemo();
    capture('gpt6_setup_click');
  }));
  document.querySelectorAll('[data-plugin]').forEach((link) => link.addEventListener('click', () => capture('gpt6_plugin_cta_click')));

  // Same email OTP endpoints and session cookie as the other treg sign-in surfaces.
  async function postSignin(path, body) {
    const response = await fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not sign in. Please try again.');
    return data;
  }
  let signinEmail = '';
  $('email-start-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('email-start-button');
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = 'Sending…';
    $('signin-error').textContent = '';
    const email = $('signin-email').value.trim();
    try {
      const data = await postSignin('/auth/email/start', { email });
      signinEmail = email;
      $('signin-recipient').textContent = email;
      $('email-start-form').hidden = true;
      $('email-verify-form').hidden = false;
      $('signin-code').value = data.dev_code || '';
      if (data.dev_code) $('signin-error').textContent = 'Dev mode — your code: ' + data.dev_code;
      $('signin-code').focus();
    } catch (error) { $('signin-error').textContent = error.message || 'Could not send the code. Please try again.'; }
    finally { button.disabled = false; button.textContent = 'Email me a sign-in code'; }
  });
  $('email-verify-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('email-verify-button');
    if (button.disabled) return;
    button.disabled = true;
    $('email-change').disabled = true;
    button.textContent = 'Verifying…';
    $('signin-error').textContent = '';
    try {
      await postSignin('/auth/email/verify', { email: signinEmail, code: $('signin-code').value.trim() });
      window.location.href = '/app?ref=gpt6';
    } catch (error) { $('signin-error').textContent = error.message || 'Could not verify the code. Please try again.'; }
    finally { button.disabled = false; $('email-change').disabled = false; button.textContent = 'Sign in'; }
  });
  $('email-change').addEventListener('click', () => {
    $('email-verify-form').hidden = true;
    $('email-start-form').hidden = false;
    $('signin-code').value = '';
    $('signin-error').textContent = '';
    $('signin-email').focus();
  });

  // A timed, self-playing workflow, following the staged provider scan on /people-search.
  // One animation clock makes pause/resume and viewport suspension deterministic.
  const demo = $('codex-demo');
  const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
  const prompt = demo.querySelector('.user-prompt');
  const tool = demo.querySelector('.tool-line');
  const board = demo.querySelector('.vendor-options');
  const scan = demo.querySelector('.vendor-scan');
  const vendors = [...demo.querySelectorAll('.vendor-option')];
  const rows = [...demo.querySelectorAll('tbody tr')];
  const cells = [...demo.querySelectorAll('.enrich')];
  const saved = demo.querySelector('.saved-action');
  const receipt = $('receipt');
  const typed = $('demo-typed');
  const composer = demo.querySelector('.composer');
  const promptText = 'Find US SaaS growth leaders. Get emails & phone numbers.';
  const captions = ['Ask for the people you need.', 'treg compares providers, side by side.', 'Your agent picks the lowest-priced option.', 'Profiles first. Then emails and phone numbers.', 'Enriching each contact across providers.', 'Found 129 matching profiles for $0.37.'];
  let elapsed = 0;
  let previousTime = null;
  let frameId = 0;
  let demoVisible = false;
  let manuallyPaused = false;
  const cycle = 16200;
  function progress(time, start, duration = 360) { return Math.max(0, Math.min(1, (time - start) / duration)); }
  function reveal(node, amount) {
    const eased = 1 - Math.pow(1 - amount, 3);
    node.style.opacity = eased;
    node.style.transform = `translateY(${(1 - eased) * 9}px)`;
  }
  function setText(node, text) { if (node.textContent !== text) node.textContent = text; }
  function cellState(cell, value, loading = false) {
    const key = `${value}:${loading}`;
    if (cell.dataset.state === key) return;
    cell.dataset.state = key;
    cell.classList.toggle('fetching', loading);
    cell.replaceChildren();
    if (loading) {
      const dot = document.createElement('span'); dot.className = 'fetch-dot';
      cell.append(dot, document.createTextNode(value));
    } else {
      cell.append(document.createTextNode(value));
      if (value && !cell.classList.contains('phone')) {
        const check = document.createElement('i'); check.textContent = '✓'; cell.append(check);
      }
    }
  }
  function paint(time) {
    const complete = time >= 12500;
    const phase = time < 2300 ? 0 : time < 4100 ? 1 : time < 5400 ? 2 : time < 7200 ? 3 : time < 12000 ? 4 : 5;
    demo.dataset.phase = String(phase);
    setText($('demo-caption'), captions[phase]);
    reveal(prompt, progress(time, 1850));
    composer.classList.toggle('typing', time < 1850);
    typed.classList.toggle('typing-caret', time >= 300 && time < 1850);
    setText(typed, time < 1850 ? promptText.slice(0, Math.floor(progress(time, 300, 1400) * promptText.length)) : 'Ask a follow-up');
    reveal(tool, progress(time, 2300));
    setText($('demo-tool-state'), time < 3200 ? 'Finding people-search tools…' : time < 4550 ? 'Comparing providers…' : time < 5400 ? 'Aviato selected' : time < 11500 ? 'Enriching contacts…' : 'People search complete');
    tool.classList.toggle('tool-loading', time >= 2300 && time < 3300);
    reveal(board, progress(time, 3100));
    vendors.forEach((vendor, i) => {
      reveal(vendor, progress(time, 3050 + i * 115));
      vendor.classList.toggle('picked', time >= 4550 && i === 0);
      vendor.classList.toggle('dimmed', time >= 4550 && i !== 0);
    });
    scan.style.opacity = time >= 3900 && time < 4550 ? '1' : '0';
    scan.style.transform = `translateY(${progress(time, 3900, 600) * (board.clientHeight - 38)}px)`;
    reveal(saved, progress(time, 4850));
    saved.classList.toggle('is-loading', time >= 4850 && time < 11500);
    setText($('demo-saving'), time < 11500 ? 'Fetching and saving to Google Sheets…' : 'Fetched and saved 129 profiles');
    rows.forEach((row, i) => reveal(row, progress(time, 5500 + i * 80, 450)));
    cells.forEach((cell, i) => {
      const row = Math.floor(i / 2);
      const start = 7300 + (row % 5) * 110;
      const end = 9500 + (row % 6) * 230 + (i % 2) * 250;
      const loading = time >= start && time < end;
      const vendor = cell.classList.contains('phone') ? (time < start + 1000 ? 'Apollo…' : 'LeadMagic…') : (time < start + 1150 ? 'Hunter…' : 'LeadMagic…');
      cellState(cell, time < start ? '' : loading ? vendor : cell.dataset.value, loading);
    });
    const tableScroll = demo.querySelector('.table-scroll');
    tableScroll.scrollTop = progress(time, 8500, 2700) * Math.max(0, tableScroll.scrollHeight - tableScroll.clientHeight);
    setText($('demo-result-state'), time < 5500 ? 'Waiting for search…' : time < 7300 ? '129 profiles found' : time < 11500 ? 'Fetching emails & phones…' : 'All contacts enriched');
    reveal(demo.querySelector('.sheet-foot'), progress(time, 11500));
    reveal(receipt, progress(time, 12000, 500));
    receipt.classList.toggle('unmarked', time < 12600);
    demo.classList.toggle('results-focus', time >= 5500 && time < 12000);
    demo.classList.toggle('demo-complete', complete);
  }
  function shouldRun() { return demoVisible && !document.hidden && !manuallyPaused && !motionPreference.matches && !$('film-dialog').open && !$('account-dialog').open; }
  function tick(now) {
    frameId = 0;
    if (!shouldRun()) { previousTime = null; return; }
    if (previousTime !== null) elapsed = (elapsed + now - previousTime) % cycle;
    previousTime = now;
    paint(elapsed);
    frameId = requestAnimationFrame(tick);
  }
  function syncDemo() {
    const running = shouldRun();
    demo.classList.toggle('demo-paused', !running);
    $('demo-pause').hidden = motionPreference.matches;
    $('demo-pause').textContent = manuallyPaused ? 'Resume animation' : 'Pause animation';
    $('demo-pause').setAttribute('aria-label', manuallyPaused ? 'Resume preview animation' : 'Pause preview animation');
    if (motionPreference.matches) paint(14000);
    if (running && !frameId) { previousTime = null; frameId = requestAnimationFrame(tick); }
    if (!running) { cancelAnimationFrame(frameId); frameId = 0; previousTime = null; }
  }
  $('demo-pause').addEventListener('click', () => { manuallyPaused = !manuallyPaused; syncDemo(); });
  document.addEventListener('visibilitychange', syncDemo);
  motionPreference.addEventListener('change', syncDemo);
  document.querySelectorAll('[data-watch]').forEach(button => button.addEventListener('click', syncDemo));
  $('film-dialog').addEventListener('close', syncDemo);
  $('account-dialog').addEventListener('close', syncDemo);
  if ('IntersectionObserver' in window) new IntersectionObserver(entries => { demoVisible = entries[0].isIntersecting; syncDemo(); }, { threshold: 0.25 }).observe(demo);
  else { demoVisible = true; syncDemo(); }
  paint(motionPreference.matches ? 14000 : 0);

  function tabs(selector, activate) {
    const buttons = [...document.querySelectorAll(selector)];
    function select(button) {
      buttons.forEach((item) => { item.setAttribute('aria-selected', String(item === button)); item.tabIndex = item === button ? 0 : -1; });
      activate(button);
    }
    buttons.forEach((button, index) => {
      button.addEventListener('click', () => select(button));
      button.addEventListener('keydown', (event) => {
        let next;
        if (event.key === 'ArrowRight') next = (index + 1) % buttons.length;
        if (event.key === 'ArrowLeft') next = (index + buttons.length - 1) % buttons.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = buttons.length - 1;
        if (next === undefined) return;
        event.preventDefault(); buttons[next].focus(); select(buttons[next]);
      });
    });
  }
  const scores = [[43, 78.2], [50.5, 80], [57, 76.3], [43.2, 62.9]];
  tabs('[data-bench]', (button) => {
    const [base, treg] = scores[Number(button.dataset.bench)];
    $('base-value').textContent = base + '%'; $('treg-value').textContent = treg + '%';
    $('base-bar').style.setProperty('--bar', base + '%'); $('treg-bar').style.setProperty('--bar', treg + '%');
    $('bench-panel').setAttribute('aria-labelledby', button.id);
  });
  const uses = {
    seo: { prompt: "Find keywords my competitors rank for that we don't. Prioritize the ones worth writing about.", title: 'Your next content opportunity.', description: 'Search volume, keyword gaps, rankings and backlinks. Turn live search data into a prioritized content plan.', sources: [['logos/dataforseo.svg', 'DataForSEO'], ['logos/serpstat.svg', 'Serpstat'], ['logos/majestic.svg', 'Majestic']], label: 'SEO' },
    geo: { prompt: 'Check how our brand shows up in AI answers. Compare us with competitors and suggest what to improve.', title: 'Find out where AI finds you.', description: 'Explore brand mentions and AI visibility across answer engines. Use the findings to improve the content your future customers discover.', sources: [['media/fable/openai.svg', 'OpenAI'], ['media/fable/perplexity-color.svg', 'Perplexity'], ['media/fable/gemini-color.svg', 'Gemini']], label: 'AI visibility' },
    social: { prompt: 'Find what our buyers are talking about on Reddit and X. Turn the strongest themes into a launch content plan.', title: 'Listen before you launch.', description: 'Read the conversations, spot recurring questions, and find creators. Bring the voice of your market into your next campaign.', sources: [['media/fable/reddit-f5f9.png', 'Reddit'], ['media/fable/x-00cb.png', 'X'], ['media/fable/tiktok-8a18.png', 'TikTok']], label: 'social' },
    ads: { prompt: 'Research our competitors’ ads. Find their strongest hooks and help me write three angles for our next campaign.', title: 'See the creative behind the campaign.', description: 'Explore ad libraries and competitor creatives. Compare messages and formats, then put the evidence into a creative brief.', sources: [['media/fable/facebook-9cf5.png', 'Meta'], ['media/fable/google-ads.png', 'Google Ads'], ['media/fable/tiktok-8a18.png', 'TikTok']], label: 'advertising' },
    recruit: { prompt: 'Find senior infrastructure engineers at Series B startups. Include their work history and available contact details.', title: 'Meet the people behind the profile.', description: 'Search by role, company and experience. Enrich the shortlist with professional context and available contact information.', sources: [['logos/aviato.svg', 'Aviato'], ['logos/apollo.svg', 'Apollo'], ['logos/pdl.svg', 'People Data Labs']], label: 'recruiting' }
  };
  let selectedUse = 'seo';
  tabs('[data-use]', (button) => {
    selectedUse = button.dataset.use;
    const data = uses[selectedUse];
    $('use-prompt').textContent = data.prompt; $('use-title').textContent = data.title; $('use-description').textContent = data.description;
    $('use-panel').setAttribute('aria-labelledby', button.id);
    $('use-link').textContent = 'Explore ' + data.label + ' tools ↗';
    $('use-sources').replaceChildren(...data.sources.map(([src, alt]) => { const image = document.createElement('img'); image.src = src; image.alt = alt; return image; }));
  });
  $('copy-prompt').addEventListener('click', () => copy('Use treg to ' + uses[selectedUse].prompt.charAt(0).toLowerCase() + uses[selectedUse].prompt.slice(1)));
})();

  /* ---------- use-case cards: each loops its mini film while on screen ---------- */
  (function () {
    var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var arr = Array.from;
    var PHONE_FROM = '+1 (•••) •••-••••', PHONE_TO = '+1 (415) 630-2214';
    /* the person and company cards play the same beats: input line, one row, then the chips */
    function lineRowChips(at, el) {
      at(250, function () { el.q('.uc-in').classList.add('in'); });
      at(850, function () { el.rows[0].classList.add('in'); });
      el.chips.forEach(function (c, i) { at(1250 + i * 260, function () { c.classList.add('in'); }); });
    }
    var anims = {
      email: function (at, el) {
        var rows = el.rows, sts = el.sts;
        rows.forEach(function (r, i) { at(200 + i * 160, function () { r.classList.add('in'); }); });
        at(1000, function () { sts[0].textContent = 'miss · $0.00'; sts[0].className = 'st miss'; rows[0].classList.add('dim'); });
        at(1550, function () { sts[1].textContent = '429 · $0.00'; sts[1].className = 'st miss'; rows[1].classList.add('dim'); });
        at(2100, function () { sts[2].textContent = '✓ $0.024'; sts[2].className = 'st hit'; rows[2].classList.add('picked'); });
        at(2500, function () { el.q('.uc-em').classList.add('in'); });
      },
      phone: function (at, el) {
        at(250, function () { el.q('.uc-in').classList.add('in'); });
        var mask = el.q('.uc-mask'), n = 0;
        for (var i = 0; i < PHONE_TO.length; i++) if (PHONE_FROM[i] !== PHONE_TO[i]) {
          n++;
          (function (upto) {
            at(750 + upto * 140, function () {
              var out = '', seen = 0;
              for (var k = 0; k < PHONE_TO.length; k++) {
                if (PHONE_FROM[k] === PHONE_TO[k]) { out += PHONE_TO[k]; continue; }
                seen++; out += seen <= upto ? PHONE_TO[k] : '•';
              }
              mask.textContent = out;
            });
          })(n);
        }
        at(2450, function () { el.chips[0].classList.add('in'); });
        at(2700, function () { el.chips[1].classList.add('in'); });
      },
      person: lineRowChips,
      company: lineRowChips,
      lookalike: function (at, el) {
        at(250, function () { el.rows[0].classList.add('in'); });
        at(600, function () { el.rows[0].classList.add('picked'); });
        el.rows.forEach(function (r, i) {
          if (i === 0) return;
          at(950 + (i - 1) * 300, function () { r.classList.add('in'); });
        });
      },
      verify: function (at, el) {
        el.rows.forEach(function (r, i) { at(200 + i * 140, function () { r.classList.add('in'); }); });
        el.rows.forEach(function (r, i) {
          at(1350 + i * 320, function () {
            var st = el.sts[i];
            if (i === 2) { st.textContent = '✕ bounced'; st.className = 'st miss'; r.classList.add('dim'); }
            else { st.textContent = '✓'; st.className = 'st hit'; }
          });
        });
        at(2900, function () { el.chips[0].classList.add('in'); });
      },
      role: function (at, el) {
        at(250, function () { el.rows[0].classList.add('in'); });
        el.rows.forEach(function (r, i) {
          if (i === 0) return;
          at(950 + (i - 1) * 280, function () { r.classList.add('in'); });
        });
        at(2100, function () { el.rows[2].classList.add('picked'); });
      },
      signals: function (at, el) {
        el.rows.forEach(function (r, i) { at(400 + i * 560, function () { r.classList.add('in'); }); });
        at(2500, function () { el.chips[0].classList.add('in'); });
      }
    };
    document.querySelectorAll('.uc[data-uc]').forEach(function (card) {
      var kind = card.getAttribute('data-uc'), fn = anims[kind];
      if (!fn) return;
      var el = {
        q: function (sel) { return card.querySelector(sel); },
        rows: arr(card.querySelectorAll('.uc-row')),
        sts: arr(card.querySelectorAll('.st')),
        chips: arr(card.querySelectorAll('.uc-chip'))
      };
      var orig = el.sts.map(function (st) { return { t: st.textContent, c: st.className }; });
      function resetCard() {
        el.rows.forEach(function (r) { r.classList.remove('in', 'dim', 'picked'); });
        el.chips.forEach(function (c) { c.classList.remove('in'); });
        ['.uc-em', '.uc-in'].forEach(function (sel) { var n = el.q(sel); if (n) n.classList.remove('in'); });
        el.sts.forEach(function (st, i) { st.textContent = orig[i].t; st.className = orig[i].c; });
        var mask = el.q('.uc-mask'); if (mask) mask.textContent = PHONE_FROM;
      }
      if (reduced) { fn(function (ms, f) { f(); }, el); return; }
      var timers = [], running = false;
      function cycle() {
        timers = [];
        resetCard();
        fn(function (ms, f) { timers.push(setTimeout(f, ms)); }, el);
        timers.push(setTimeout(cycle, 5600));
      }
      var io2 = new IntersectionObserver(function (es) {
        es.forEach(function (e) {
          if (e.isIntersecting && !running) { running = true; cycle(); }
          else if (!e.isIntersecting && running) { running = false; timers.forEach(clearTimeout); timers = []; }
        });
      }, { threshold: 0.3 });
      io2.observe(card);
    });
  })();
