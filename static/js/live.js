// Live updates by polling: every so often, re-fetch this page and swap in the sections marked `data-live`
// (each needs a stable id). Sections the user is interacting with are left alone until they're done.
(function () {
  "use strict";
  var INTERVAL_MS = 20000;
  var MAX_INTERVAL_MS = 5 * 60000;
  var delay = INTERVAL_MS;
  var timer = null;

  function regions() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-live][id]"));
  }

  function isBusy(el) {
    if (el.contains(document.activeElement) && document.activeElement !== document.body) return true;
    // Don't clobber anything the user has typed or ticked but not submitted yet.
    return Array.prototype.some.call(el.querySelectorAll("input, textarea, select"), function (f) {
      if (f.type === "checkbox" || f.type === "radio") return f.checked !== f.defaultChecked;
      if (f.tagName === "SELECT") return Array.prototype.some.call(f.options, function (o) { return o.selected !== o.defaultSelected; });
      if (f.type === "hidden") return false;
      if (f.type === "file") return f.files.length > 0;
      return f.value !== f.defaultValue;
    });
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(refresh, delay);
  }

  function refresh() {
    if (document.hidden) return; // resumes on visibilitychange
    fetch(window.location.href, { headers: { "X-Live-Refresh": "1" }, cache: "no-store", credentials: "same-origin" })
      .then(function (resp) {
        // Logged out or page gone: stop polling rather than swapping in a login page.
        if (resp.redirected || resp.status === 404 || resp.status === 403) { delay = null; return null; }
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then(function (html) {
        if (html === null) return;
        var fresh = new DOMParser().parseFromString(html, "text/html");
        regions().forEach(function (el) {
          var next = fresh.getElementById(el.id);
          if (next && !isBusy(el) && next.innerHTML !== el.innerHTML) el.innerHTML = next.innerHTML;
        });
        delay = INTERVAL_MS;
      })
      .catch(function () { delay = Math.min(delay * 2, MAX_INTERVAL_MS); }) // back off while the server is unhappy
      .then(function () { if (delay) schedule(); });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && delay) refresh();
  });
  if (regions().length) schedule();
})();
