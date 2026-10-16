// The dock in the hero: the chips switch what its six keys show (each key is a tile of one picture; the new
// picture cross-fades in on all six at once and the knobs turn a little), and until someone picks one it steps
// through them by itself. Plus: the download links follow the newest release.
(function () {
  var dock = document.getElementById('dock');
  var chips = Array.prototype.slice.call(document.querySelectorAll('.screens button'));
  if (!dock || !chips.length) return;
  var knobs = Array.prototype.slice.call(dock.querySelectorAll('.knob'));
  var onB = false, cur = 0, turn = 0, timer = null;
  var still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

  function show(i) {
    if (i === cur) return;
    var chip = chips[i], src = chip.getAttribute('data-src');
    chips.forEach(function (c, k) { c.setAttribute('aria-pressed', k === i ? 'true' : 'false'); });
    cur = i;
    turn += (i % 2 ? 1 : -1) * 38;
    knobs.forEach(function (k, n) { k.style.transform = 'rotate(' + (turn * (n ? .6 : 1)) + 'deg)'; });
    // load it first, so the keys never fade to black; the hidden layer (--a under the keys, --b on top) takes the
    // new picture and the top layer fades in or out to reveal it
    var img = new Image();
    img.onload = function () {
      if (cur !== i) return;                       // a newer pick came in while this one loaded
      dock.style.setProperty(onB ? '--a' : '--b', 'url("' + src + '")');
      onB = !onB;
      dock.classList.toggle('b', onB);
      dock.setAttribute('aria-label', chip.getAttribute('data-alt'));
    };
    img.src = src;
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
