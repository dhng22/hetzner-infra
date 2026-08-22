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

  // --- environment editor ---------------------------------------------------
  // Two views of one form: a row per variable, or the whole thing as text so a
  // .env can be pasted in one go. They are never both live — switching converts
  // the edits across and disables the view that just left the screen, so the
  // form submits exactly what was on it. The server does the same conversion
  // (envstore.parse_bulk), so the rules have to match; keep them in step.

  function addVarRow(grid, key, value) {
    var k = document.createElement("input");
    k.type = "text";
    k.name = "key";
    k.className = "k-in";
    k.placeholder = "NEW_VARIABLE";
    k.setAttribute("aria-label", "Variable name");
    k.value = key || "";

    var v = document.createElement("input");
    v.type = "text";
    v.name = "value";
    v.placeholder = "value";
    v.setAttribute("aria-label", "Value");
    v.value = value || "";

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn-sm btn-danger";
    remove.setAttribute("data-remove-var", "");
    remove.textContent = "Remove";

    grid.appendChild(k);
    grid.appendChild(v);
    grid.appendChild(remove);
    return k;
  }

  function envParts(panel) {
    if (!panel) { return null; }
    var grid = panel.querySelector("[data-kv]");
    var area = panel.querySelector("textarea[name=bulk]");
    return grid && area ? { grid: grid, area: area } : null;
  }

  function rowsToText(grid) {
    var out = [];
    grid.querySelectorAll('input[name="key"]').forEach(function (k) {
      var key = k.value.trim();
      var value = k.nextElementSibling;
      if (key) { out.push(key + "=" + (value ? value.value : "")); }
    });
    return out.length ? out.join("\n") + "\n" : "";
  }

  function textToRows(grid, text) {
    // Everything but the two column headings, which are plain divs.
    grid.querySelectorAll("input, [data-remove-var]").forEach(function (el) {
      el.remove();
    });
    text.split(/\r\n|\r|\n/).forEach(function (raw) {
      var line = raw.trim();
      if (!line || line.charAt(0) === "#") { return; }
      if (line.indexOf("export ") === 0) { line = line.slice(7).replace(/^\s+/, ""); }
      var eq = line.indexOf("=");
      // A line with no '=' is kept as a name with no value rather than dropped,
      // so a mistyped paste is visible instead of quietly missing. Saving it
      // fails on the server's name check, which says what is wrong with it.
      if (eq === -1) { addVarRow(grid, line, ""); return; }
      addVarRow(grid, line.slice(0, eq).trim(), line.slice(eq + 1).trim());
    });
  }

  function setEnvMode(panel, mode) {
    var parts = envParts(panel);
    if (!parts) { return; }
    var text = mode === "text";

    if (text) { parts.area.value = rowsToText(parts.grid); }
    else { textToRows(parts.grid, parts.area.value); }

    // hidden AND disabled, on both sides. Hidden is for the eye; disabled is
    // what keeps the browser from submitting the view you are not looking at,
    // which is the whole reason the two can never disagree.
    parts.grid.hidden = text;
    parts.grid.querySelectorAll("input").forEach(function (i) { i.disabled = text; });
    parts.area.hidden = !text;
    parts.area.disabled = !text;

    panel.querySelectorAll("[data-mode-only]").forEach(function (el) {
      el.hidden = el.getAttribute("data-mode-only") !== mode;
    });
    panel.querySelectorAll("[data-env-mode]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-env-mode") === mode));
    });
    try { localStorage.setItem("panel-env-mode", mode); } catch (e) { /* private mode */ }
  }

  // --- one listener --------------------------------------------------------
  document.addEventListener("click", function (ev) {
    var el;

    if ((el = ev.target.closest("[data-theme-toggle]"))) {
      ev.preventDefault();
      applyTheme(currentTheme() === "dark" ? "light" : "dark");
      return;
    }

    if ((el = ev.target.closest("[data-toggle-master]"))) {
      var section = el.closest("[data-toggle-section]");
      if (section) { syncToggleSection(section); }
      return;
    }

    if ((el = ev.target.closest("[data-tab]"))) {
      // Some tabs are rendered empty until the server is asked for them, so
      // switching to one in the browser would show a blank panel and then
      // rewrite the URL to look like it had loaded. Let the link navigate.
      if (el.hasAttribute("data-tab-server")) { return; }
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
      var addTo = el.closest("section, [data-panel]").querySelector("[data-kv]");
      if (addTo) { addVarRow(addTo).focus(); }
      return;
    }

    if ((el = ev.target.closest("[data-env-mode]"))) {
      ev.preventDefault();
      setEnvMode(el.closest("section, [data-panel]"), el.getAttribute("data-env-mode"));
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

  // --- destructive actions --------------------------------------------------
  // A form carrying data-confirm asks before it submits. Deliberately native
  // confirm(): the panel has no modal, and a hand-rolled one that fails open on
  // a JS error would be worse than the browser's.
  document.addEventListener("submit", function (ev) {
    var form = ev.target.closest("form[data-confirm]");
    if (!form) { return; }
    if (!window.confirm(form.getAttribute("data-confirm"))) {
      ev.preventDefault();
      ev.stopImmediatePropagation();
    }
  }, true);

  // Preview builds have no server. Say so instead of appearing to do nothing.
  document.addEventListener("submit", function (ev) {
    var form = ev.target.closest("form[data-preview]");
    if (!form) { return; }
    ev.preventDefault();
    var btn = form.querySelector("button[type=submit]") || form.querySelector("button");
    if (btn) { flashButton(btn, "Preview only"); }
  });

  // Which view you last used, remembered per browser. The preview renders this
  // partial once per app, hence querySelectorAll rather than a single lookup.
  try {
    if (localStorage.getItem("panel-env-mode") === "text") {
      document.querySelectorAll("[data-env-modes]").forEach(function (g) {
        setEnvMode(g.closest("section, [data-panel]"), "text");
      });
    }
  } catch (e) { /* private mode */ }

  // Sections rendered server-side already carry their checkbox state; this
  // brings the disabled state of their controls into line with it on load.
  initToggleSections(document);

  // --- timestamps in the reader's own timezone ------------------------------
  // The server renders UTC, because the server does not know where you are and
  // a wrong local time is worse than an explicit UTC one. Once here, we do
  // know: every <time data-localtime> is rewritten to the browser's zone, and
  // the full ISO value stays in the title for when the exact instant matters.
  function localiseTimes(root) {
    var nodes = (root || document).querySelectorAll("time[data-localtime]");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var raw = el.getAttribute("datetime");
      if (!raw) { continue; }
      // Bare ISO strings with no zone are UTC here; say so or the browser reads
      // them as local and the correction is applied twice.
      var iso = /(?:Z|[+-]\d{2}:?\d{2})$/.test(raw) ? raw : raw + "Z";
      var when = new Date(iso);
      if (isNaN(when.getTime())) { continue; }
      try {
        el.textContent = when.toLocaleString(undefined, {
          year: "numeric", month: "short", day: "2-digit",
          hour: "2-digit", minute: "2-digit"
        });
        el.title = when.toString();
      } catch (e) { /* leave the server's UTC text alone */ }
    }
  }
  localiseTimes(document);

  // --- cluster map: keep it current -----------------------------------------
  // Rendered server-side first, so the section is complete and correct with JS
  // off; this only refreshes it in place.
  //
  // The timer only exists while someone is actually looking. Two conditions,
  // both required: the browser tab is visible, AND the map is scrolled into
  // view. A background tab polling a Docker socket every five seconds forever
  // is pure waste, and so is polling a section sitting below the fold.
  var TOPO_MS = 5000;

  function pct(v) { return v === null || v === undefined ? "—" : Math.round(v) + "%"; }

  // Signature of a node's task list. Cheap way to know whether the blocks need
  // rebuilding at all — on a quiet cluster they never do, so a tick touches
  // nothing and there is no flicker.
  // State and reservations are part of the signature, not just the task list:
  // a chip whose task went from running to failed, or whose reservation was
  // re-sized, looks identical by name and would never be repainted otherwise.
  // A section whose title carries its own on/off switch. With it off the
  // controls inside are disabled — they are not merely irrelevant, they are
  // describing a policy nothing will read, and a form that accepts input it
  // will ignore is how you end up tuning thresholds that never applied.
  //
  // Disabled inputs submit nothing, which is exactly right here: the spec is
  // merged, so an untouched policy keeps whatever it already said.
  function syncToggleSection(section) {
    var master = section.querySelector("[data-toggle-master]");
    var body = section.querySelector("[data-toggle-body]");
    if (!master || !body) { return; }
    var on = master.checked;
    section.classList.toggle("is-off", !on);
    var controls = body.querySelectorAll("input, select, textarea, button");
    for (var i = 0; i < controls.length; i++) { controls[i].disabled = !on; }
    var label = section.querySelector(".switch-label");
    if (label) { label.textContent = on ? "on" : "off"; }
  }

  function initToggleSections(root) {
    var sections = (root || document).querySelectorAll("[data-toggle-section]");
    for (var i = 0; i < sections.length; i++) { syncToggleSection(sections[i]); }
  }

  function sig(tasks) {
    var s = "";
    for (var i = 0; i < tasks.length; i++) {
      var t = tasks[i];
      s += t.name + "|" + t.key + "|" + t.tone + "|" + t.cpu_share + "|" + t.mem_share + ";";
    }
    return s;
  }

  function buildSlot(t) {
    var el = document.createElement("span");
    el.className = "slot slot-" + t.key;
    el.tabIndex = 0;
    // The ring is CPU reserved as a share of this node; the fill behind the
    // label is memory. Both are set as custom properties so the drawing stays
    // entirely in the stylesheet.
    el.style.setProperty("--cpu", t.cpu_share);
    el.style.setProperty("--mem", t.mem_share);
    el.title = t.service + " — task " + t.id + "\nstate: " + t.state +
               "\nreserves " + t.cpu_share + "% of this node's CPU, " +
               t.mem_share + "% of its memory";
    var dot = document.createElement("i");
    dot.className = "dot dot-" + t.tone;      // what it is DOING; the chip tint
    el.appendChild(dot);                      // already says what it IS
    // textContent, never innerHTML: service names come from the daemon and are
    // not this file's to trust with markup.
    el.appendChild(document.createTextNode(t.name));
    return el;
  }

  function paintTopology(data) {
    var host = document.querySelector("[data-topo-rows]");
    if (!host || !data || !data.nodes) { return; }

    var rows = host.querySelectorAll(".tree-branch");
    if (rows.length !== data.nodes.length) { location.reload(); return; }

    // root of the tree
    var total = 0;
    for (var k = 0; k < data.nodes.length; k++) { total += data.nodes[k].tasks_total; }
    var setRoot = function (sel, text) {
      var el = host.querySelector(".tnode-root " + sel);
      if (el && el.textContent !== text) { el.textContent = text; }
    };
    setRoot('[data-f="hosts"]', String(data.nodes.length));
    setRoot('[data-f="total"]', String(total));

    for (var i = 0; i < data.nodes.length; i++) {
      var n = data.nodes[i];
      var row = host.querySelector('[data-node="' + n.id + '"]');
      if (!row) { location.reload(); return; }   // fleet changed shape

      var set = function (sel, text) {
        var el = row.querySelector(sel);
        if (el && el.textContent !== text) { el.textContent = text; }
      };
      set('[data-f="tasks"]', String(n.tasks_total));
      set('[data-f="cpu"]', pct(n.cpu_pct));
      set('[data-f="mem"]', pct(n.mem_pct));

      // The reserved rings. Utilisation moves constantly and reservation
      // barely ever, but when it does it is the number that decides whether a
      // server gets bought, so it has to be live too.
      var gauge = function (sel, value, label, absolute) {
        var el = row.querySelector(sel);
        if (!el) { return; }
        el.style.setProperty("--v", value == null ? 0 : value);
        el.title = label + " reserved: " + (value == null ? "—" : value + "%") +
                   (absolute ? " — " + absolute : "");
      };
      gauge('[data-f="cpures"]', n.cpu_reserved_pct, "CPU",
            n.cpu_reserved + " of " + n.cpus + " vCPU promised to tasks here");
      gauge('[data-f="memres"]', n.mem_reserved_pct, "Memory",
            n.mem_reserved_mb + " MB of " + n.memory_gb + " GB promised to tasks here");

      var slots = row.querySelector("[data-slots]");
      if (!slots) { continue; }
      var next = sig(n.tasks);
      if (slots.getAttribute("data-sig") === next) { continue; }   // unchanged
      slots.setAttribute("data-sig", next);

      var frag = document.createDocumentFragment();
      if (!n.tasks.length) {
        var empty = document.createElement("span");
        empty.className = "slot is-empty";
        empty.textContent = "no running tasks";
        frag.appendChild(empty);
      } else {
        for (var j = 0; j < n.tasks.length; j++) { frag.appendChild(buildSlot(n.tasks[j])); }
      }
      slots.replaceChildren(frag);
    }
  }

  function markLive(state) {
    var el = document.querySelector("[data-topo-live]");
    if (!el) { return; }
    el.classList.toggle("is-stale", state === "stale");
    el.classList.toggle("is-idle", state === "paused");
    el.lastChild.nodeValue = state;
  }

  var topo = document.querySelector("[data-topology]");
  if (topo && window.fetch) {
    var timer = null;
    // When we can observe visibility, assume NOT on screen until the observer
    // says otherwise — so a load that lands with the map below the fold makes
    // no request at all. Without observer support, fall back to "visible".
    var onScreen = !window.IntersectionObserver;
    var everRan = false;

    var fetchOnce = function () {
      fetch("/api/topology", { headers: { "Accept": "application/json" },
                               credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (d) { paintTopology(d); markLive("live"); })
        .catch(function () { markLive("stale"); });  // preview build, or logged out
    };

    var sync = function () {
      if (onScreen && !document.hidden) {
        if (timer !== null) { return; }
        // The server rendered this markup moments ago, so the first start has
        // nothing to catch up on. Every later resume does.
        if (everRan) { fetchOnce(); } else { everRan = true; }
        markLive("live");
        timer = setInterval(fetchOnce, TOPO_MS);
      } else if (timer !== null) {
        clearInterval(timer);                // stop the timer, not just its body
        timer = null;
        markLive("paused");
      }
    };

    if (window.IntersectionObserver) {
      new IntersectionObserver(function (entries) {
        onScreen = entries[0].isIntersecting;
        sync();
      }, { rootMargin: "120px" }).observe(topo);
    }
    document.addEventListener("visibilitychange", sync);
    sync();
  }
})();
