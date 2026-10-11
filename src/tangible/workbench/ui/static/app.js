// Small progressive enhancements. Every page works without this file.
(function () {
  "use strict";
  // Guard against double submits: mark the form busy (CSS dims its buttons). The submitter is
  // not disabled, so its name/value still reaches the server.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
    if (form.hasAttribute("data-busy")) { e.preventDefault(); return; }
    form.setAttribute("data-busy", "");
  });
  // htmx forms are swapped out on success; on a failed request, release the form.
  document.addEventListener("htmx:afterRequest", function (e) {
    var form = e.target && e.target.closest ? e.target.closest("form") : null;
    if (form) form.removeAttribute("data-busy");
  });
  // Coming back via the back/forward cache: forms are usable again.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-busy]").forEach(function (f) { f.removeAttribute("data-busy"); });
  });
})();
