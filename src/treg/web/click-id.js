// First-party click capture and same-origin carry. No third-party request.
(function () {
  try {
    var kinds = ['gclid', 'gbraid', 'wbraid'];
    var current = new URL(window.location.href);
    var q = current.searchParams;
    var kind = kinds.find(function (k) { return q.get(k); });
    var id = kind && q.get(kind);
    var landing = '';
    var fromUrl = !!id;
    if (id) {
      // utm_content carries the ORIGINAL landing across pages and browser hops. It must precede
      // data-page, which describes the destination rather than the page the ad originally opened.
      landing = q.get('utm_content') || (document.body && document.body.dataset.page) || q.get('ref') || '';
      document.cookie = 'treg_ad=' + encodeURIComponent(kind + '|' + id + '|' + landing) +
        ';path=/;max-age=7776000;samesite=lax' + (current.protocol === 'https:' ? ';secure' : '');
    } else {
      var cookie = document.cookie.split(';').find(function (v) { return v.trim().indexOf('treg_ad=') === 0; });
      if (!cookie) return;
      var parts = decodeURIComponent(cookie.trim().slice(8)).split('|');
      if (parts.length >= 3 && kinds.indexOf(parts[0]) !== -1) {
        kind = parts[0]; id = parts[1]; landing = parts.slice(2).join('|');
      } else if (parts.length === 2) {
        kind = 'gclid'; id = parts[0]; landing = parts[1];
      } else return;
    }
    if (!id) return;
    // URL carry belongs to the ad visit, not every future visit during the cookie's 90 days.
    // Match the marker to this click so another tab's newer cookie cannot inherit an older visit.
    var activeVisit = fromUrl;
    try {
      var marker = kind + '|' + id;
      if (fromUrl) window.sessionStorage.setItem('treg_ad_visit', marker);
      else activeVisit = window.sessionStorage.getItem('treg_ad_visit') === marker;
    } catch (e) { /* blocked storage still permits carry from this page's URL */ }
    if (!activeVisit) return;
    function carry(url) {
      url.searchParams.set(kind, id);
      // Reuse the established landing convention only when needed to preserve its value.
      if (landing && url.searchParams.get('utm_content') !== landing) {
        url.searchParams.set('utm_content', landing);
      }
    }
    carry(current);
    window.history.replaceState(window.history.state, '', current.href);
    // The dashboard rewrites its own address (for example to /app#start) and would drop the id,
    // which an in-app browser's "open in browser" then loses. Same-origin only, never another click.
    ['pushState', 'replaceState'].forEach(function (name) {
      var original = window.history[name];
      if (typeof original !== 'function') return;
      window.history[name] = function (state, title, url) {
        try {
          if (url != null) {
            var next = new URL(url, window.location.href);
            if (next.origin === current.origin && !kinds.some(function (k) { return next.searchParams.has(k); })) {
              carry(next);
              url = next.href;
            }
          }
        } catch (e) { /* never break navigation for attribution */ }
        return original.call(this, state, title, url);
      };
    });
    function rewrite(event) {
      try {
        var a = event.target.closest && event.target.closest('a[href]');
        if (!a) return;
        var href = a.getAttribute('href');
        if (!href || href.trim().charAt(0) === '#') return;
        var target = new URL(href, document.baseURI);
        if (target.origin !== current.origin || !/^https?:$/.test(target.protocol)) return;
        if (kinds.some(function (k) { return target.searchParams.has(k); })) return;
        carry(target);
        a.href = target.href;
      } catch (e) { /* never break navigation for attribution */ }
    }
    // Capture phase runs before SPA link handlers; delegation includes dynamically added links.
    document.addEventListener('click', rewrite, true);
    document.addEventListener('auxclick', rewrite, true);
  } catch (e) { /* never break the page for a marketing cookie */ }
})();
