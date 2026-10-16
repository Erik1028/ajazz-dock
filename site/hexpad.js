// The dock in the hero: the chips switch what its keys show (cross-fading two images, turning the knobs a
// little), and until someone picks one it steps through them by itself. Plus: the download links follow the
// newest release.
(function () {
  var a = document.getElementById('screen-a'), b = document.getElementById('screen-b');
  var chips = Array.prototype.slice.call(document.querySelectorAll('.screens button'));
  var knobs = Array.prototype.slice.call(document.querySelectorAll('.dock .knob'));
  if (!a || !b || !chips.length) return;
  var front = a, back = b, cur = 0, turn = 0, timer = null;
  var still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

  function show(i) {
    if (i === cur) return;
    var chip = chips[i], src = chip.getAttribute('data-src');
    chips.forEach(function (c, k) { c.setAttribute('aria-pressed', k === i ? 'true' : 'false'); });
    cur = i;
    turn += (i % 2 ? 1 : -1) * 38;
    knobs.forEach(function (k, n) { k.style.transform = 'rotate(' + (turn * (n ? .6 : 1)) + 'deg)'; });
    back.onload = function () {
      back.onload = null;
      back.alt = chip.getAttribute('data-alt'); back.removeAttribute('aria-hidden');
      front.alt = ''; front.setAttribute('aria-hidden', 'true');
      back.style.opacity = 1; front.style.opacity = 0;
      // keep the visible image in normal flow so the screen keeps its size
      back.style.position = 'relative'; front.style.position = 'absolute';
      var t = front; front = back; back = t;
    };
    back.src = src;
    if (back.complete && back.naturalWidth && back.getAttribute('src') === src) back.onload();
  }

  chips.forEach(function (c, i) {
    c.addEventListener('click', function () { stop(); show(i); });
  });
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  if (!still) {
    timer = setInterval(function () {
      if (document.hidden) return;
      var r = document.getElementById('dock').getBoundingClientRect();
      if (r.bottom < 0 || r.top > (window.innerHeight || 800)) return;   // only while it's on screen
      show((cur + 1) % chips.length);
    }, 3800);
  }
})();

(function () {
  // The download links point at the newest release's Hexpad zip. The HTML already carries 2.0.0's link, version
  // and size, so a failed or rate-limited request (60 an hour per visitor) leaves those in place.
  function mb(n) { return n >= 1048576 * 10 ? Math.round(n / 1048576) + ' MB' : (Math.round(n / 104857.6) / 10) + ' MB'; }
  fetch('https://api.github.com/repos/Erik1028/ajazz-dock/releases?per_page=10')
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (list) {
      if (!list) return;
      var rel = list.filter(function (x) { return !x.draft && !x.prerelease; })[0];
      if (!rel) return;
      var asset = (rel.assets || []).filter(function (x) { return /hexpad.*\.(exe|zip)$/i.test(x.name); })[0];
      if (!asset) return;
      ['dl', 'dl2'].forEach(function (id) {
        var a = document.getElementById(id);
        if (!a) return;
        a.href = asset.browser_download_url;
        var f = a.querySelector('.fname'), sz = a.querySelector('.size');
        if (f) f.textContent = asset.name;
        if (sz && asset.size) sz.textContent = ' (' + mb(asset.size) + ')';
      });
      var ver = document.getElementById('ver');
      var d = rel.published_at ? new Date(rel.published_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) : '';
      if (ver) ver.textContent = rel.tag_name + (d ? ' · ' + d : '');
    })
    .catch(function () {});
})();
