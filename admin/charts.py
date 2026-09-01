"""
Charts, as SVG strings, computed from series and nothing else.

Pure derivation, like `shape.py` and for the same reason: the panel is rendered
by two interchangeable data modules and drawn by one set of templates, so
anything that computes rather than fetches has to live where both can reach it
and a unit test can pin it.

DRAWN ON THE SERVER, deliberately. The panel has no charting library, no CDN
reference and no `<script>` beyond its own file, and the live-refresh mechanism
it already has (`data-live-html`) swaps in server-rendered markup. Charts drawn
here therefore refresh themselves with no new client code at all — and the
"which HTML changed" comparison that stops the DOM flickering keeps working,
because two identical readings produce two identical strings.

COLOUR IS NOT CHOSEN HERE. The four series hues are the categorical palette
already validated for both themes in `static/style.css` — `--s-prod`,
`--s-staging`, `--s-data`, `--s-observe`, with `--s-platform` as the neutral
tail. The comment above them explains that their ORDER is the check, so this
module reads them in that order and never adds a fifth. Status colours
(`--ok`/`--warn`/`--bad`) mean status here too, and are used only where a chart
is actually reporting a threshold.
"""

import html

#: The categorical series hues, in the order the stylesheet validated them.
#: A fifth series folds into the neutral rather than inventing a hue.
SERIES_VARS = ("--s-prod", "--s-staging", "--s-data", "--s-observe")
NEUTRAL_VAR = "--s-platform"

#: One aspect for every chart in the column, so a stack of them reads as a set
#: rather than as five unrelated pictures. Width is arbitrary — the SVG scales
#: to its container — but the RATIO is what the eye actually judges.
W = 320
H = 96
PAD_L, PAD_R, PAD_T, PAD_B = 2, 2, 6, 12


def series_var(index):
    return SERIES_VARS[index] if index < len(SERIES_VARS) else NEUTRAL_VAR


def _esc(text):
    return html.escape(str(text), quote=True)


def fmt(value, unit=""):
    """
    A number short enough to sit on a chart.

    Thousands separators are deliberately absent: these sit in 40px of space
    next to a line, and `12.4k` reads there where `12,431` does not.
    """
    if value is None:
        return "—"
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        text = f"{value / 1_000_000:.1f}M"
    elif magnitude >= 1_000:
        text = f"{value / 1_000:.1f}k"
    elif magnitude >= 100:
        text = f"{value:.0f}"
    elif magnitude >= 1:
        text = f"{value:.1f}"
    elif magnitude == 0:
        text = "0"
    else:
        text = f"{value:.2f}"
    return f"{text}{unit}"


def _empty(label):
    """
    What a chart with no data draws.

    Not an empty box and not a flat line at zero: "no series matched" and "the
    value is zero" are different facts, and a chart that draws them the same way
    is the reason someone spends an afternoon debugging a healthy system.
    """
    return (f'<div class="chart-empty">{_esc(label)}</div>')


def _frame(body, title, extra_class=""):
    classes = ("chart " + extra_class).strip()
    return (f'<svg class="{classes}" viewBox="0 0 {W} {H}" role="img" '
            f'preserveAspectRatio="none" aria-label="{_esc(title)}">'
            f'<title>{_esc(title)}</title>{body}</svg>')


def _scale(series, floor_zero=True, headroom=1.08):
    """
    (lo, hi) for the value axis, never a zero-height one.

    A flat series is the common case on a quiet cluster, and dividing by a range
    of zero is how a chart becomes a stack trace. A flat series is given a band
    around itself so it draws as a straight line in the middle, which is what it
    is.
    """
    values = [v for points in series.values() for _, v in points]
    if not values:
        return 0.0, 1.0
    hi = max(values) * headroom
    lo = 0.0 if floor_zero else min(values)
    if hi - lo < 1e-9:
        return (lo - 1.0, hi + 1.0) if not floor_zero else (0.0, max(hi, 1.0))
    return lo, hi


def _span(series):
    """(first, last) timestamp across every series, or None."""
    stamps = [t for points in series.values() for t, _ in points]
    return (min(stamps), max(stamps)) if stamps else None


def _points(points, t0, t1, lo, hi):
    """Series points as SVG user-space coordinates, clamped inside the box."""
    width = (W - PAD_L - PAD_R)
    height = (H - PAD_T - PAD_B)
    tspan = (t1 - t0) or 1
    vspan = (hi - lo) or 1
    out = []
    for t, v in points:
        x = PAD_L + width * (t - t0) / tspan
        # Clamped rather than dropped: a reading above the axis is real, and a
        # chart that silently omits it draws a gap where the spike was.
        y = PAD_T + height * (1 - min(1.0, max(0.0, (v - lo) / vspan)))
        out.append((x, y))
    return out


def _path(coords):
    return " ".join(("M" if i == 0 else "L") + f"{x:.1f} {y:.1f}"
                    for i, (x, y) in enumerate(coords))


def line(series, unit="", reference=None, band=None, empty="no data yet"):
    """
    One line per series over time.

    `reference` draws a dashed rule at a value — the SLO a latency is judged
    against, the ratio an alert fires at. `band` shades from that value upward,
    for the case where being above it is the thing that matters.
    """
    series = {k: v for k, v in (series or {}).items() if v}
    if not series:
        return _empty(empty)

    span = _span(series)
    t0, t1 = span
    lo, hi = _scale(series)
    if reference is not None:
        hi = max(hi, reference * 1.15)

    body = []
    height = H - PAD_T - PAD_B
    if band is not None and hi > lo:
        y = PAD_T + height * (1 - min(1.0, max(0.0, (band - lo) / (hi - lo))))
        body.append(f'<rect class="chart-band" x="{PAD_L}" y="{PAD_T:.1f}" '
                    f'width="{W - PAD_L - PAD_R}" height="{max(0.0, y - PAD_T):.1f}"/>')
    if reference is not None and hi > lo:
        y = PAD_T + height * (1 - min(1.0, max(0.0, (reference - lo) / (hi - lo))))
        body.append(f'<line class="chart-ref" x1="{PAD_L}" y1="{y:.1f}" '
                    f'x2="{W - PAD_R}" y2="{y:.1f}"/>')

    for index, (name, points) in enumerate(sorted(series.items())):
        coords = _points(points, t0, t1, lo, hi)
        body.append(f'<path class="chart-line" style="stroke:var({series_var(index)})" '
                    f'd="{_path(coords)}"><title>{_esc(name)}</title></path>')

    peak = max(v for points in series.values() for _, v in points)
    body.append(f'<text class="chart-tick" x="{PAD_L}" y="{H - 2}">'
                f'{_esc(fmt(peak, unit))} peak</text>')
    return _frame("".join(body), f"{len(series)} series, peak {fmt(peak, unit)}")


def stack(series, unit="", empty="no traffic recorded"):
    """
    Stacked areas — for a total split into parts, where the parts sum to
    something meaningful. Response codes are the case this exists for: the
    height IS the request rate and the colours are what it was made of.
    """
    series = {k: v for k, v in (series or {}).items() if v}
    if not series:
        return _empty(empty)

    span = _span(series)
    t0, t1 = span
    order = sorted(series.items())
    # Stack on a shared time axis, so every layer has a point at every stamp
    # the chart draws. Interpolating would invent readings; carrying the last
    # known value forward states plainly that nothing new arrived.
    stamps = sorted({t for _, points in order for t, _ in points})
    running = {t: 0.0 for t in stamps}
    layers = []
    for name, points in order:
        lookup = dict(points)
        last = 0.0
        upper = []
        lower = []
        for t in stamps:
            last = lookup.get(t, last)
            lower.append((t, running[t]))
            running[t] += last
            upper.append((t, running[t]))
        layers.append((name, lower, upper))

    hi = max(running.values()) * 1.08 or 1.0
    body = []
    for index, (name, lower, upper) in enumerate(layers):
        top = _points(upper, t0, t1, 0.0, hi)
        bottom = list(reversed(_points(lower, t0, t1, 0.0, hi)))
        path = _path(top) + " " + " ".join(f"L{x:.1f} {y:.1f}" for x, y in bottom) + " Z"
        body.append(f'<path class="chart-area" style="fill:var({series_var(index)})" '
                    f'd="{path}"><title>{_esc(name)}</title></path>')

    peak = max(running.values())
    body.append(f'<text class="chart-tick" x="{PAD_L}" y="{H - 2}">'
                f'{_esc(fmt(peak, unit))} peak</text>')
    return _frame("".join(body), f"{len(layers)} layers, peak {fmt(peak, unit)}")


def bars(rows, unit="", empty="nothing to compare"):
    """
    Horizontal bars, one per named thing, longest first — not a time series.

    Used where the question is "which of these is worst right now", which is
    what utilisation across nodes actually asks. A row may carry its own `tone`
    (`ok`/`warn`/`bad`) when it is being judged against a threshold; without one
    it takes the neutral hue, because a bar with no threshold is not a verdict.

    `rows` is `[{"name", "value", "max"?, "tone"?, "note"?}]`.
    """
    rows = [r for r in (rows or []) if r.get("value") is not None]
    if not rows:
        return _empty(empty)

    ceiling = max([r.get("max") or r["value"] for r in rows] + [1e-9])
    out = ['<div class="bar-rows">']
    for row in sorted(rows, key=lambda r: -r["value"]):
        share = max(0.0, min(1.0, row["value"] / (row.get("max") or ceiling)))
        tone = row.get("tone") or ""
        note = row.get("note") or fmt(row["value"], unit)
        out.append(
            f'<div class="bar-row{(" is-" + tone) if tone else ""}">'
            f'<span class="bar-name" title="{_esc(row["name"])}">{_esc(row["name"])}</span>'
            f'<span class="bar-track"><i style="width:{share * 100:.1f}%"></i></span>'
            f'<span class="bar-value">{_esc(note)}</span>'
            f'</div>')
    out.append('</div>')
    return "".join(out)


def columns(rows, unit="", empty="none in this window"):
    """
    Vertical bars for counts over a window — error tallies, not rates.

    A count of zero is drawn as a visible baseline rather than as nothing, so
    "we counted and it was none" is distinguishable from "we did not look".
    """
    rows = [r for r in (rows or []) if r.get("value") is not None]
    if not rows:
        return _empty(empty)

    hi = max([r["value"] for r in rows] + [1.0])
    slot = (W - PAD_L - PAD_R) / max(1, len(rows))
    bar = min(46.0, slot * 0.62)
    height = H - PAD_T - PAD_B
    body = []
    for index, row in enumerate(rows):
        share = row["value"] / hi
        drawn = max(1.5, height * share)          # zero still shows a baseline
        x = PAD_L + slot * index + (slot - bar) / 2
        tone = row.get("tone") or ""
        style = f'style="fill:var({series_var(index)})"' if not tone else ""
        body.append(f'<rect class="chart-col{(" is-" + tone) if tone else ""}" {style} '
                    f'x="{x:.1f}" y="{PAD_T + height - drawn:.1f}" '
                    f'width="{bar:.1f}" height="{drawn:.1f}" rx="2">'
                    f'<title>{_esc(row["name"])}: {_esc(fmt(row["value"], unit))}</title>'
                    f'</rect>')
        body.append(f'<text class="chart-tick" x="{x + bar / 2:.1f}" y="{H - 2}" '
                    f'text-anchor="middle">{_esc(fmt(row["value"], unit))}</text>')
    return _frame("".join(body), ", ".join(
        f"{r['name']} {fmt(r['value'], unit)}" for r in rows))


def bullet(value, warn=None, danger=None, ceiling=100.0, unit="%",
           empty="not reporting"):
    """
    One measurement against the thresholds that will act on it.

    A gauge shows a number; a bullet shows the number NEXT TO the line somebody
    wrote down. That is the useful form for cluster saturation, where the
    interesting fact is not "58%" but "58%, and 70% is where it starts buying
    machines".
    """
    if value is None:
        return _empty(empty)

    ceiling = max(ceiling, value) or 1.0
    tone = ""
    if danger is not None and value >= danger:
        tone = "bad"
    elif warn is not None and value >= warn:
        tone = "warn"

    marks = []
    for threshold, kind in ((warn, "warn"), (danger, "bad")):
        if threshold is None:
            continue
        marks.append(f'<i class="bullet-mark is-{kind}" '
                     f'style="left:{min(100.0, threshold / ceiling * 100):.1f}%"></i>')
    return (f'<div class="bullet{(" is-" + tone) if tone else ""}">'
            f'<span class="bullet-track">'
            f'<i class="bullet-fill" style="width:{min(100.0, value / ceiling * 100):.1f}%"></i>'
            f'{"".join(marks)}</span>'
            f'<span class="bullet-value">{_esc(fmt(value, unit))}</span></div>')
