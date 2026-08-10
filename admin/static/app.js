/* ==========================================================================
   Panel behaviour.

   Everything is ONE delegated listener on document. That is deliberate: the
   first version bound handlers per element from inside a partial, and the
   preview includes that partial once per app, so a single "Add variable" click
   fired sixteen times. Delegation makes duplicate binding impossible no matter
   how many times the markup appears.
   ========================================================================== */

(function () {
  "use strict";

  var root = document.documentElement;
  var body = document.body;

  // --- theme ---------------------------------------------------------------
  // The class goes on <body>, not <html>. Custom properties set on body cascade
  // to every component and cannot be overridden by a host page that manages
  // data-theme on the root itself — which is why the toggle looked dead when
  // the preview was viewed as a hosted artifact.
  function currentTheme() {
    if (body.classList.contains("theme-light")) { return "light"; }
    if (body.classList.contains("theme-dark")) { return "dark"; }
    var attr = root.getAttribute("data-theme");
    if (attr === "light" || attr === "dark") { return attr; }
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light" : "dark";
  }

  function applyTheme(name) {
    body.classList.toggle("theme-light", name === "light");
    body.classList.toggle("theme-dark", name === "dark");
    root.setAttribute("data-theme", name);
    try { localStorage.setItem("panel-theme", name); } catch (e) { /* private mode */ }
  }

  try {
    var saved = localStorage.getItem("panel-theme");
    if (saved === "light" || saved === "dark") { applyTheme(saved); }
  } catch (e) { /* private mode */ }

  // --- helpers -------------------------------------------------------------
  function scopeOf(el) {
    return el.closest("[data-view]") || document;
  }

  function flashButton(btn, text) {
    var original = btn.textContent;
    btn.textContent = text;
    btn.classList.add("copied");
    setTimeout(function () {
      btn.textContent = original;
      btn.classList.remove("copied");
    }, 1400);
  }

  function copyText(text, btn) {
    // The panel is reachable over plain http on the private address, where
    // navigator.clipboard is undefined — so the textarea fallback is the path
    // that actually runs half the time, not a legacy nicety.
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () { flashButton(btn, "Copied"); },
        function () { legacyCopy(text, btn); }
      );
      return;
    }
    legacyCopy(text, btn);
  }

  function legacyCopy(text, btn) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    flashButton(btn, ok ? "Copied" : "Press Ctrl+C");
  }

  function addVarRow(panel) {
    var grid = panel.querySelector("[data-kv]");
    if (!grid) { return; }
    var key = document.createElement("input");
    key.type = "text";
    key.name = "key";
    key.className = "k-in";
    key.placeholder = "NEW_VARIABLE";
    key.setAttribute("aria-label", "Variable name");

    var value = document.createElement("input");
    value.type = "text";
    value.name = "value";
    value.placeholder = "value";
    value.setAttribute("aria-label", "Value");

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn-sm btn-danger";
    remove.setAttribute("data-remove-var", "");
    remove.textContent = "Remove";

    grid.appendChild(key);
    grid.appendChild(value);
    grid.appendChild(remove);
    key.focus();
  }

  // --- one listener --------------------------------------------------------
  document.addEventListener("click", function (ev) {
    var el;

    if ((el = ev.target.closest("[data-theme-toggle]"))) {
      ev.preventDefault();
      applyTheme(currentTheme() === "dark" ? "light" : "dark");
      return;
    }

    if ((el = ev.target.closest("[data-tab]"))) {
      ev.preventDefault();
      var group = el.closest("[data-tabs]");
      var scope = scopeOf(el);
      var panels = scope.querySelector("[data-panels]");
      if (!group || !panels) { return; }
      var name = el.getAttribute("data-tab");
      group.querySelectorAll("[data-tab]").forEach(function (t) {
        t.setAttribute("aria-selected", String(t === el));
      });
      panels.querySelectorAll(":scope > [data-panel]").forEach(function (p) {
        p.hidden = p.getAttribute("data-panel") !== name;
      });
      // Keep the URL shareable, without a round trip.
      if (el.href && el.href.indexOf("#") === -1 && window.history.replaceState) {
        window.history.replaceState(null, "", el.href);
      }
      return;
    }

    if ((el = ev.target.closest("[data-add-var]"))) {
      ev.preventDefault();
      addVarRow(el.closest("section, [data-panel]"));
      return;
    }

    if ((el = ev.target.closest("[data-remove-var]"))) {
      ev.preventDefault();
      var value = el.previousElementSibling;
      var key = value && value.previousElementSibling;
      if (key) { key.remove(); }
      if (value) { value.remove(); }
      el.remove();
      return;
    }

    if ((el = ev.target.closest("[data-copy]"))) {
      ev.preventDefault();
      copyText(el.getAttribute("data-copy-text") || "", el);
      return;
    }

    if ((el = ev.target.closest("[data-reveal]"))) {
      ev.preventDefault();
      var target = el.parentElement.querySelector("[data-secret-text]");
      if (!target) { return; }
      var nowHidden = target.classList.toggle("masked");
      target.textContent = nowHidden
        ? "••••••••••••"
        : target.getAttribute("data-secret-text");
      el.textContent = nowHidden ? "Reveal" : "Hide";
      return;
    }
  });

  // Preview builds have no server. Say so instead of appearing to do nothing.
  document.addEventListener("submit", function (ev) {
    var form = ev.target.closest("form[data-preview]");
    if (!form) { return; }
    ev.preventDefault();
    var btn = form.querySelector("button[type=submit]") || form.querySelector("button");
    if (btn) { flashButton(btn, "Preview only"); }
  });
})();
