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
  // A form carrying data-confirm asks before it submits.
  //
  // This used to be native confirm(), on the reasoning that a hand-rolled modal
  // failing open on a JS error is worse than the browser's. That reasoning is
  // kept, not dropped: `ask()` returns false if it cannot build or open the
  // dialog, and the caller then falls back to window.confirm() — the submit is
  // blocked either way, and there is no path where a delete goes through
  // unasked.
  //
  // <dialog>.showModal() is what makes it worth doing at all: focus trapping,
  // Escape-to-cancel and inertness of the page behind come from the platform
  // rather than from three more listeners here.
  var confirmBox = null;

  function buildConfirmBox() {
    var d = document.createElement("dialog");
    d.className = "modal";
    d.innerHTML =
      '<form method="dialog" class="modal-card">' +
        '<div class="modal-ico" data-ico></div>' +
        '<div class="modal-text">' +
          '<h2 data-title></h2>' +
          '<p data-body></p>' +
        '</div>' +
        '<div class="modal-actions">' +
          '<button class="btn" value="cancel" type="submit">Cancel</button>' +
          '<button class="btn" value="ok" type="submit" data-ok></button>' +
        '</div>' +
      '</form>';
    document.body.appendChild(d);
    return d;
  }

  // Two rounded glyphs, both drawn into a circle by the stylesheet: a warning
  // for anything that changes state, a bin for anything that destroys it.
  var ICONS = {
    warn: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.6 1.8 20.4h20.4L12 3.6Z" ' +
          'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>' +
          '<path d="M12 10v4.4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>' +
          '<circle cx="12" cy="17.4" r="1.05" fill="currentColor"/></svg>',
    danger: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M10 7V5.2h4V7m-7.4 0 .9 12.2' +
            'a1.6 1.6 0 0 0 1.6 1.5h5.8a1.6 1.6 0 0 0 1.6-1.5L17.4 7" fill="none" ' +
            'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>' +
            '<path d="M10.4 11v6M13.6 11v6" stroke="currentColor" stroke-width="1.8" ' +
            'stroke-linecap="round"/></svg>'
  };

  // "Stop api? Its files are kept." -> title "Stop api?", body the rest. The
  // confirm strings are already written as a question followed by the
  // consequence, so this needs no second attribute on every form.
  function splitPrompt(text) {
    var m = /^(.+?[?.!])\s+(.*)$/.exec(text.trim());
    return m ? [m[1], m[2]] : [text.trim(), ""];
  }

  function ask(form, message, onOk) {
    try {
      if (typeof HTMLDialogElement !== "function" ||
          typeof document.createElement("dialog").showModal !== "function") {
        return false;
      }
      confirmBox = confirmBox || buildConfirmBox();
      var danger = !!form.querySelector("button.btn-danger, .btn-danger");
      var parts = splitPrompt(message);
      var ok = confirmBox.querySelector("[data-ok]");

      confirmBox.querySelector("[data-ico]").innerHTML = danger ? ICONS.danger : ICONS.warn;
      confirmBox.querySelector("[data-ico]").className = "modal-ico " + (danger ? "is-danger" : "is-warn");
      confirmBox.querySelector("[data-title]").textContent = parts[0];
      var body = confirmBox.querySelector("[data-body]");
      body.textContent = parts[1];
      body.hidden = !parts[1];
      ok.textContent = danger ? "Yes, continue" : "Confirm";
      ok.className = "btn " + (danger ? "btn-danger" : "btn-primary");

      confirmBox.onclose = function () {
        var chose = confirmBox.returnValue === "ok";
        confirmBox.returnValue = "";
        if (chose) { onOk(); }
      };
      confirmBox.showModal();
      // Cancel takes focus, not the destructive button: Enter on a dialog you
      // did not read should do nothing.
      confirmBox.querySelector("button[value=cancel]").focus();
      return true;
    } catch (e) {
      return false;
    }
  }

  document.addEventListener("submit", function (ev) {
    var form = ev.target.closest("form[data-confirm]");
    if (!form) { return; }

    // Second pass, after the dialog said yes. Let it through untouched so the
    // other submit listeners (the preview notice) still see it.
    if (form.getAttribute("data-confirmed") === "1") {
      form.removeAttribute("data-confirmed");
      return;
    }

    var message = form.getAttribute("data-confirm");
    var submitter = ev.submitter || null;

    // requestSubmit() re-fires the submit event, which form.submit() does not —
    // without it the preview notice would never run on a confirmed form.
    if (typeof form.requestSubmit !== "function") {
      if (!window.confirm(message)) {
        ev.preventDefault();
        ev.stopImmediatePropagation();
      }
      return;
    }

    ev.preventDefault();
    ev.stopImmediatePropagation();

    var go = function () {
      form.setAttribute("data-confirmed", "1");
      form.requestSubmit(submitter);
    };
    if (!ask(form, message, go)) {
      if (window.confirm(message)) { go(); }
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
  syncPlacement(document);

  // --- tooltips that appear immediately -------------------------------------
  // The native `title` attribute waits about a second before showing, which is
  // useless for a gauge you are sweeping across to compare nodes. This is one
  // element parented to <body> rather than a CSS ::after on the trigger,
  // because the cluster map scrolls inside an overflow container and an
  // absolutely positioned tooltip would be clipped by it.
  var tipEl = null;

  function hideTip() {
    if (tipEl) { tipEl.classList.remove("is-on"); }
  }

  function showTip(target) {
    var text = target.getAttribute("data-tip");
    if (!text) { return; }
    if (!tipEl) {
      tipEl = document.createElement("div");
      tipEl.className = "tip";
      tipEl.setAttribute("role", "tooltip");
      document.body.appendChild(tipEl);
    }
    tipEl.textContent = text;                    // never innerHTML
    tipEl.classList.add("is-on");

    var r = target.getBoundingClientRect();
    var t = tipEl.getBoundingClientRect();
    var left = r.left + r.width / 2 - t.width / 2;
    var top = r.top - t.height - 8;
    if (top < 4) { top = r.bottom + 8; }         // flip under when it would clip
    left = Math.max(6, Math.min(left, window.innerWidth - t.width - 6));
    tipEl.style.transform = "translate(" + Math.round(left) + "px," + Math.round(top) + "px)";
  }

  document.addEventListener("mouseover", function (ev) {
    var el = ev.target.closest ? ev.target.closest("[data-tip]") : null;
    if (el) { showTip(el); }
  });
  document.addEventListener("mouseout", function (ev) {
    if (ev.target.closest && ev.target.closest("[data-tip]")) { hideTip(); }
  });
  document.addEventListener("focusin", function (ev) {
    var el = ev.target.closest ? ev.target.closest("[data-tip]") : null;
    if (el) { showTip(el); }
  });
  document.addEventListener("focusout", hideTip);
  window.addEventListener("scroll", hideTip, true);

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
  // Default cadence. A panel that costs more to render than a socket walk —
  // the observability column is a dozen range queries — declares its own with
  // `data-live-ms`, so one expensive section cannot force everything else to be
  // slow and cannot be forgotten about either.
  var LIVE_MS = 5000;

  function cadence(el) {
    var want = parseInt(el.getAttribute("data-live-ms"), 10);
    return want > 0 ? want : LIVE_MS;
  }

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

  // The manager switch and the placement mode are one decision wearing two
  // hats, and the server keeps them in step whatever this does — see
  // Component.normalize. This is here so the form does not sit there showing
  // you a combination it is about to change behind your back: turning the
  // manager on sets Placement to auto in front of you, and pinning Placement
  // turns the manager off.
  //
  // Which one wins depends on which one you just touched, exactly as on the
  // server: the more recent, more specific intent is the one that survives.
  function syncPlacement(root) {
    var scope = root || document;
    var master = scope.querySelector("[data-placement-sync]");
    var mode = scope.querySelector("#f-placement_mode");
    if (!master || !mode) { return; }
    master.addEventListener("change", function () {
      if (master.checked && mode.value !== "auto") { mode.value = "auto"; }
    });
    mode.addEventListener("change", function () {
      if (mode.value !== "auto" && master.checked) {
        master.checked = false;
        var section = master.closest("[data-toggle-section]");
        if (section) { syncToggleSection(section); }
      }
    });
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
    el.setAttribute("data-tip", t.service + " — task " + t.id +
                    " · state: " + t.state +
                    " · reserves " + t.cpu_share + "% of this node's CPU and " +
                    t.mem_share + "% of its memory");
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
        el.setAttribute("data-tip",
                        label + " reserved: " + (value == null ? "—" : value + "%") +
                        (absolute ? " — " + absolute : ""));
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

  // The indicator belongs to the panel it reports on. It used to be looked up
  // globally, which was fine while exactly one section was live and wrong the
  // moment a second one was: every panel would have driven the same dot.
  function indicatorFor(host) {
    return function (state) {
      var el = (host || document).querySelector("[data-topo-live]")
            || document.querySelector("[data-topo-live]");
      if (!el) { return; }
      el.classList.toggle("is-stale", state === "stale");
      el.classList.toggle("is-idle", state === "paused");
      el.lastChild.nodeValue = state;
    };
  }

  var markLive = indicatorFor(null);

  // Poll `tick` while `el` is genuinely being looked at: the browser tab
  // visible AND the element on screen. Extracted so the Map tab gets exactly
  // the same restraint the Overview map has — a background tab hitting the
  // Docker socket every five seconds forever is pure waste, and so is polling a
  // panel sitting below the fold or behind another tab.
  function livePoll(el, tick, onPause) {
    if (!el || !window.fetch) { return; }
    var timer = null;
    // Where we can observe visibility, assume NOT on screen until the observer
    // says otherwise, so a load that lands below the fold makes no request at
    // all. Without observer support, fall back to "visible".
    var onScreen = !window.IntersectionObserver;
    var everRan = false;

    var sync = function () {
      // `hidden` also covers a panel on a tab you are not looking at: the tab
      // strip sets it, so switching away stops the timer without any coupling
      // between the two mechanisms.
      var showing = onScreen && !document.hidden && !el.hasAttribute("hidden");
      if (showing) {
        if (timer !== null) { return; }
        // The server rendered this markup moments ago, so the first start has
        // nothing to catch up on. Every later resume does.
        if (everRan) { tick(); } else { everRan = true; }
        if (onPause) { onPause("live"); }
        timer = setInterval(tick, cadence(el));
      } else if (timer !== null) {
        clearInterval(timer);                // stop the timer, not just its body
        timer = null;
        if (onPause) { onPause("paused"); }
      }
    };

    if (window.IntersectionObserver) {
      new IntersectionObserver(function (entries) {
        onScreen = entries[0].isIntersecting;
        sync();
      }, { rootMargin: "120px" }).observe(el);
    }
    document.addEventListener("visibilitychange", sync);
    // Tab switching toggles `hidden` rather than firing an event, so watch the
    // attribute directly instead of teaching the tab code about polling.
    if (window.MutationObserver) {
      new MutationObserver(sync).observe(el, { attributes: true,
                                               attributeFilter: ["hidden"] });
    }
    sync();
    return sync;
  }

  livePoll(document.querySelector("[data-topology]"), function () {
    fetch("/api/topology", { headers: { "Accept": "application/json" },
                             credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (d) { paintTopology(d); markLive("live"); })
      .catch(function () { markLive("stale"); });  // preview build, or logged out
  }, markLive);

  // A panel that re-renders itself from the server. The Overview map has a JSON
  // feed and a painter; this one swaps the SERVER'S own markup in, so the two
  // views of the same data cannot disagree about how a block is drawn — there
  // is only one place that draws it.
  //
  // Replaced only when the HTML actually differs, so a quiet cluster never
  // touches the DOM and nothing flickers under the cursor.
  var fragments = document.querySelectorAll("[data-live-html]");
  for (var fi = 0; fi < fragments.length; fi++) {
    (function (host) {
      var url = host.getAttribute("data-live-html");
      var mark = indicatorFor(host);
      livePoll(host, function () {
        fetch(url, { headers: { "Accept": "text/html" }, credentials: "same-origin" })
          .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
          .then(function (html) {
            if (html !== host.getAttribute("data-live-cache")) {
              host.setAttribute("data-live-cache", html);
              host.innerHTML = html;
              localiseTimes(host);
            }
            mark("live");
          })
          .catch(function () { mark("stale"); });
      }, mark);
    })(fragments[fi]);
  }

  // --- logs: follow the tail ------------------------------------------------
  // Not `data-live-html`: that REPLACES a panel, and a log pane has to APPEND
  // or every poll would throw away your scroll position and the selection you
  // were making. The server hands back only what is new, keyed on a cursor, so
  // a quiet service costs one empty response every few seconds.
  (function () {
    var pane = document.querySelector("[data-logs]");
    if (!pane) { return; }
    var stream = pane.querySelector("[data-logs-stream]");
    var button = document.querySelector("[data-logs-toggle]");
    var mark = indicatorFor(pane);
    var url = pane.getAttribute("data-logs-url");
    var cursor = pane.getAttribute("data-logs-cursor") || "";
    var limit = parseInt(pane.getAttribute("data-logs-limit"), 10) || 200;
    var paused = false;
    var busy = false;

    // Follow only while the reader is ALREADY at the bottom. Yanking someone
    // back down because a line arrived while they were reading history is the
    // behaviour that makes people turn tailing off.
    function pinned() {
      return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 8;
    }

    function append(html) {
      var wasPinned = pinned();
      stream.insertAdjacentHTML("beforeend", html);
      while (stream.children.length > limit) { stream.removeChild(stream.firstChild); }
      if (wasPinned) { stream.scrollTop = stream.scrollHeight; }
    }

    function tick() {
      if (paused || busy || !cursor) { return; }
      busy = true;
      fetch(url + (url.indexOf("?") < 0 ? "?" : "&") + "since=" + encodeURIComponent(cursor),
            { headers: { "Accept": "text/html" }, credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
        .then(function (html) {
          var next = /^<!--cursor:(\d+)-->/.exec(html);
          if (next) { cursor = next[1]; html = html.slice(next[0].length); }
          if (html.trim()) { append(html); }
          mark("live");
        })
        .catch(function () { mark("stale"); })
        .then(function () { busy = false; });
    }

    if (button) {
      button.hidden = false;
      button.addEventListener("click", function () {
        paused = !paused;
        button.textContent = paused ? "Resume" : "Pause";
        button.setAttribute("aria-pressed", paused ? "true" : "false");
        mark(paused ? "paused" : "live");
        // Resuming asks from the same cursor, so the gap fills in rather than
        // being skipped: pausing stops the screen moving, not the recording.
        if (!paused) { tick(); }
      });
    }
    // Open at the newest line. The pane is scrolled to the top by default and
    // the newest line is at the bottom, which is why this tab always needed a
    // scroll before it said anything.
    stream.scrollTop = stream.scrollHeight;
    livePoll(pane, tick, function (state) { mark(paused ? "paused" : state); });
  })();

  // --- the visualiser wait page ---------------------------------------------
  // It used to say "give it a few seconds and reload". The reason it had to was
  // a bug that is now fixed on the server; this is the other half — the page
  // asks, instead of the person.
  (function () {
    var wait = document.querySelector("[data-viewer-wait]");
    if (!wait || !window.fetch) { return; }
    var mark = indicatorFor(wait);
    var status = wait.getAttribute("data-status-url");
    var open = wait.getAttribute("data-open-url");
    var timer = setInterval(function () {
      fetch(status, { headers: { "Accept": "application/json" },
                      credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (d) {
          if (d && d.ready) { clearInterval(timer); window.location.href = open; }
        })
        .catch(function () { clearInterval(timer); mark("stale"); });
    }, 2000);
  })();
})();
