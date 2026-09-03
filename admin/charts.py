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

AXES ARE HTML, THE PLOT IS SVG. The plot carries `preserveAspectRatio="none"`
so it stretches to whatever width the column gives it, which stretches any
`<text>` inside it by the same factor. So the SVG holds geometry only and every
label — the value axis, the time axis, the legend — is HTML laid out around it,
in the page's own type. The one-line READING that says where a chart is now is
handed to the card instead of drawn here, so the whole column's summaries share
one box and one position.

HOVER COSTS NO JAVASCRIPT. Each sample gets an invisible full-height slice
carrying `data-tip`, and `static/app.js` already owns a delegated, no-delay
tooltip parented to <body>. Delegation is what makes it survive the live poll:
a listener bound to a chart element would be thrown away with the element the
next time the fragment is replaced.

COLOUR IS NOT CHOSEN HERE. The four series hues are the categorical palette
already validated for both themes in `static/style.css` — `--s-prod`,
`--s-staging`, `--s-data`, `--s-observe`, with `--s-platform` as the neutral
tail. The comment above them explains that their ORDER is the check, so this
module reads them in that order and never adds a fifth. Status colours
(`--ok`/`--warn`/`--bad`) mean status here too, and are used only where a chart
is actually reporting a threshold.
"""

import html
import math

#: The categorical series hues, in the order the stylesheet validated them.
#: A fifth series folds into the neutral rather than inventing a hue.
SERIES_VARS = ("--s-prod", "--s-staging", "--s-data", "--s-observe")
NEUTRAL_VAR = "--s-platform"

#: One aspect for every chart in the column, so a stack of them reads as a set
#: rather than as five unrelated pictures. Width is arbitrary — the SVG scales
#: to its container — but the RATIO is what the eye actually judges.
W = 320
H = 96
PAD_L, PAD_R, PAD_T, PAD_B = 2, 2, 6, 4

#: How many series are NAMED in a legend before the rest are only counted.
#: Past the fourth there is no distinct hue left to name them by, so a fifth
#: legend row would claim a distinction the picture does not make.
LEGEND_MAX = 4

#: Axis tops somebody would write down. A raw peak of 5407 gives an axis
#: labelled "5.4k", which reads as a measurement rather than as a scale; the
#: ladder is fine-grained enough that rounding up never wastes half the height.
_NICE_STEPS = (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10)


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


def ago(stamp, latest):
    """
    How far back a sample sits, relative to the newest one in the same chart.

    Relative rather than a clock time, and that is not laziness: this module is
    pure, the server renders UTC, and the reader is somewhere else. Everywhere
    the panel prints an absolute instant it hands the conversion to the browser
    (`<time data-localtime>`), which a `data-tip` attribute cannot do. "12 min
    ago" is true in every timezone.
    """
    minutes = int(round((latest - stamp) / 60.0))
    if minutes <= 0:
        return "now"
    if minutes < 90:
        return f"{minutes} min ago"
    return f"{minutes / 60.0:.1f} h ago"


def _nice(value):
    """A round number at or above `value`, for an axis top that reads as one."""
    if value <= 0:
        return 1.0
    magnitude = 10.0 ** math.floor(math.log10(value))
    for step in _NICE_STEPS:
        if value <= step * magnitude * (1 + 1e-9):
            return step * magnitude
    return 10.0 * magnitude


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


def _worst(series):
    """(name, points) of the series with the highest peak, or (None, None)."""
    if not series:
        return None, None
    name = max(series, key=lambda k: max(v for _, v in series[k]))
    return name, series[name]


# --- readings ----------------------------------------------------------------
# The one line worth reading under a chart. These RETURN it rather than drawing
# it: every summary in the column is printed by the card, in one bordered box
# under the picture, so the reader has a single place to look and a card with no
# chart at all (a bullet, a rollup) can carry one too. Phrasing stays here
# because it is the same phrasing question as an axis label; WHICH summary a
# card gets is decided in `shape.observability`, beside the card.


def reading(series, unit="", reference=None, named=True):
    """
    Where a time chart IS — now, against its own peak and against its rule.

    Not a caption naming the axes: the tick labels on both edges already say
    what the planes are. These are the numbers a shape cannot show, and the ones
    somebody scanning a column of charts is actually after.
    """
    name, points = _worst(series)
    if not points:
        return ""
    latest = points[-1][1]
    peak = max(v for _, v in points)
    parts = []
    if named and len(series) > 1:
        parts.append(f"{name} highest")
    parts.append(f"now {fmt(latest, unit)}")
    if peak > latest:
        parts.append(f"peak {fmt(peak, unit)}")
    if reference:
        over = latest >= reference
        parts.append(f"{'OVER' if over else 'under'} the {fmt(reference, unit)} line")
    return " · ".join(parts)


def mix(series, unit=""):
    """
    What a stacked total is now, and which layer it is mostly made of.

    The stacked height IS the total, which the picture already shows. What it
    cannot say is which colour that total belongs to, and for status codes that
    is the whole question.
    """
    series = {k: v for k, v in (series or {}).items() if v}
    if not series:
        return ""
    stamps, held = _carried(series)
    totals = {t: sum(row[t] for row in held.values()) for t in stamps}
    newest = stamps[-1]
    peak = max(totals.values())
    parts = [f"now {fmt(totals[newest], unit)}"]
    if peak > totals[newest]:
        parts.append(f"peak {fmt(peak, unit)}")
    biggest = max(held, key=lambda n: held[n][newest])
    if held[biggest][newest] > 0:
        parts.append(f"mostly {biggest}")
    return " · ".join(parts)


def busiest(rows, unit=""):
    """Which row of a bar chart is closest to its own ceiling."""
    rows = [r for r in (rows or []) if r.get("value") is not None]
    if not rows:
        return ""
    ceiling = max([r.get("max") or r["value"] for r in rows] + [1e-9])
    worst = max(rows, key=lambda r: r["value"] / (r.get("max") or ceiling))
    return f'busiest: {worst["name"]} at {fmt(worst["value"], unit)}'


def tally(rows, unit=""):
    """
    What a column chart counted, in words.

    "We counted and it was none" is the answer most of the time on an error
    tally, and it is worth saying: a row of baselines looks identical to a chart
    that failed to load.
    """
    rows = [r for r in (rows or []) if r.get("value") is not None]
    if not rows:
        return ""
    hits = sorted((r for r in rows if r["value"] > 0), key=lambda r: -r["value"])
    if not hits:
        return "nothing counted in this window"
    return "counted: " + ", ".join(f'{r["name"]} {fmt(r["value"], unit)}'
                                   for r in hits)


def _carried(series):
    """
    Every series sampled at every stamp any of them has, last value carried.

    Stacking needs a value for each layer at each stamp. Interpolating would
    invent readings; carrying the last known one states plainly that nothing new
    arrived. `mix()` reads the same table the picture is built from, so the
    summary and the stack cannot disagree about what the total is.
    """
    stamps = sorted({t for points in series.values() for t, _ in points})
    held = {}
    for name, points in series.items():
        lookup = dict(points)
        last = 0.0
        held[name] = {}
        for t in stamps:
            last = lookup.get(t, last)
            held[name][t] = last
    return stamps, held


def _ticks(values):
    return "".join(
        f"<span>{_esc(v[0])}<b>{_esc(v[1])}</b></span>" if isinstance(v, tuple)
        else f"<span>{_esc(v)}</span>" for v in values)


def _wrap(plot, y_ticks=(), x_ticks=(), legend=(), spread=False):
    """
    The plot, plus the furniture that says what it is measuring.

    A picture of a line with no scale on either edge is a shape, not a
    measurement. Everything that makes it one lives out here: the two ends of
    the value axis, the two ends of the time axis, and which colour is which
    series. The reading itself does NOT — it is printed by the card, so every
    summary in the column sits in one box in one place.
    """
    plain = "" if y_ticks else " is-plain"
    out = [f'<figure class="chart-wrap{plain}">']
    if y_ticks:
        out.append(f'<div class="chart-y">{_ticks(y_ticks)}</div>')
    out.append(plot)
    if x_ticks:
        klass = "chart-x is-spread" if spread else "chart-x"
        out.append(f'<div class="{klass}">{_ticks(x_ticks)}</div>')
    if legend:
        out.append('<div class="chart-legend">' + "".join(
            f'<span class="chart-key"><i style="background:var({var})"></i>'
            f'{_esc(name)}</span>' for name, var in legend) + '</div>')
    out.append('</figure>')
    return "".join(out)


def _legend(names):
    items = [(name, series_var(i)) for i, name in enumerate(names[:LEGEND_MAX])]
    if len(names) > LEGEND_MAX:
        items.append((f"+{len(names) - LEGEND_MAX} more", NEUTRAL_VAR))
    return items


def _scale(series):
    """
    (lo, hi) for the value axis, never a zero-height one.

    A flat series is the common case on a quiet cluster, and dividing by a range
    of zero is how a chart becomes a stack trace. A flat series is given a band
    above itself so it draws as a straight line partway up, which is what it is.
    """
    values = [v for points in series.values() for _, v in points]
    if not values:
        return 0.0, 1.0
    return 0.0, _nice(max(values))


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


def _slices(rows, t0, t1, unit):
    """
    One invisible full-height column per sample, carrying its own readout.

    This is the entire hover mechanism. `data-tip` is already handled globally
    by `static/app.js` — one tooltip parented to <body>, shown with no delay —
    so a chart gets a readout without a line of chart-specific JavaScript, and
    it keeps working after the live poll replaces the markup underneath it,
    which a listener bound to a chart element would not.

    The slice spans the midpoints to either side, so every pixel of the plot
    belongs to exactly one sample and there is nowhere to hover that answers
    nothing.
    """
    if not rows:
        return ""
    width = W - PAD_L - PAD_R
    tspan = (t1 - t0) or 1
    xs = [PAD_L + width * (t - t0) / tspan for t, _ in rows]
    out = ['<g class="chart-slices">']
    for index, (stamp, readings) in enumerate(rows):
        left = PAD_L if index == 0 else (xs[index - 1] + xs[index]) / 2
        right = (W - PAD_R) if index == len(rows) - 1 \
            else (xs[index] + xs[index + 1]) / 2
        tip = ago(stamp, t1)
        if readings:
            tip += " · " + ", ".join(f"{n} {fmt(v, unit)}" for n, v in readings)
        out.append(f'<rect class="chart-slice" x="{left:.1f}" y="{PAD_T}" '
                   f'width="{max(0.5, right - left):.1f}" '
                   f'height="{H - PAD_T - PAD_B}" data-tip="{_esc(tip)}"/>')
    out.append('</g>')
    return "".join(out)


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

    t0, t1 = _span(series)
    lo, hi = _scale(series)
    if reference is not None:
        hi = max(hi, _nice(reference * 1.15))

    body = []
    height = H - PAD_T - PAD_B
    if band is not None and hi > lo:
        edge = PAD_T + height * (1 - min(1.0, max(0.0, (band - lo) / (hi - lo))))
        body.append(f'<rect class="chart-band" x="{PAD_L}" y="{PAD_T:.1f}" '
                    f'width="{W - PAD_L - PAD_R}" '
                    f'height="{max(0.0, edge - PAD_T):.1f}"/>')
    if reference is not None and hi > lo:
        edge = PAD_T + height * (1 - min(1.0, max(0.0, (reference - lo) / (hi - lo))))
        body.append(f'<line class="chart-ref" x1="{PAD_L}" y1="{edge:.1f}" '
                    f'x2="{W - PAD_R}" y2="{edge:.1f}"/>')

    order = sorted(series.items())
    for index, (name, points) in enumerate(order):
        coords = _points(points, t0, t1, lo, hi)
        body.append(f'<path class="chart-line" style="stroke:var({series_var(index)})" '
                    f'd="{_path(coords)}"><title>{_esc(name)}</title></path>')

    lookups = {name: dict(points) for name, points in order}
    stamps = sorted({t for _, points in order for t, _ in points})
    # A series with no reading at this stamp is LEFT OUT of the readout rather
    # than carried forward: a line chart draws a straight segment across a gap
    # because it has to join two points, but the tooltip would be inventing a
    # measurement that was never taken.
    body.append(_slices(
        [(t, [(n, lookups[n][t]) for n, _ in order if t in lookups[n]])
         for t in stamps], t0, t1, unit))

    peak = max(v for points in series.values() for _, v in points)
    return _wrap(
        _frame("".join(body), f"{len(series)} series, peak {fmt(peak, unit)}"),
        y_ticks=(fmt(hi, unit), fmt(lo, unit)),
        x_ticks=(ago(t0, t1), "now"),
        legend=_legend([name for name, _ in order]))


def stack(series, unit="", empty="no traffic recorded"):
    """
    Stacked areas — for a total split into parts, where the parts sum to
    something meaningful. Response codes are the case this exists for: the
    height IS the request rate and the colours are what it was made of.
    """
    series = {k: v for k, v in (series or {}).items() if v}
    if not series:
        return _empty(empty)

    t0, t1 = _span(series)
    order = sorted(series.items())
    stamps, own = _carried(series)
    running = {t: 0.0 for t in stamps}
    layers = []
    for name, _ in order:
        upper = []
        lower = []
        for t in stamps:
            lower.append((t, running[t]))
            running[t] += own[name][t]
            upper.append((t, running[t]))
        layers.append((name, lower, upper))

    hi = _nice(max(running.values()))
    body = []
    for index, (name, lower, upper) in enumerate(layers):
        top = _points(upper, t0, t1, 0.0, hi)
        bottom = list(reversed(_points(lower, t0, t1, 0.0, hi)))
        path = _path(top) + " " + " ".join(f"L{x_:.1f} {y_:.1f}"
                                           for x_, y_ in bottom) + " Z"
        body.append(f'<path class="chart-area" style="fill:var({series_var(index)})" '
                    f'd="{path}"><title>{_esc(name)}</title></path>')

    # The readout names each LAYER's own value, not its stacked height. The
    # height is what the picture already shows; what it cannot show is which
    # part of it belongs to which colour.
    body.append(_slices(
        [(t, [(name, own[name][t]) for name, _ in order] + [("total", running[t])])
         for t in stamps], t0, t1, unit))

    peak = max(running.values())
    return _wrap(
        _frame("".join(body), f"{len(layers)} layers, peak {fmt(peak, unit)}"),
        y_ticks=(fmt(hi, unit), fmt(0.0, unit)),
        x_ticks=(ago(t0, t1), "now"),
        legend=_legend([name for name, _ in order]))


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
        top = row.get("max") or ceiling
        share = max(0.0, min(1.0, row["value"] / top))
        tone = row.get("tone") or ""
        note = row.get("note") or fmt(row["value"], unit)
        tip = f'{row["name"]}: {fmt(row["value"], unit)} of {fmt(top, unit)}'
        out.append(
            f'<div class="bar-row{(" is-" + tone) if tone else ""}" '
            f'data-tip="{_esc(tip)}">'
            f'<span class="bar-name" title="{_esc(row["name"])}">{_esc(row["name"])}</span>'
            f'<span class="bar-track"><i style="width:{share * 100:.1f}%"></i></span>'
            f'<span class="bar-value">{_esc(note)}</span>'
            f'</div>')
    out.append('</div>')
    return _wrap("".join(out))


def columns(rows, unit="", empty="none in this window"):
    """
    Vertical bars for counts over a window — error tallies, not rates.

    A count of zero is drawn as a visible baseline rather than as nothing, so
    "we counted and it was none" is distinguishable from "we did not look".
    """
    rows = [r for r in (rows or []) if r.get("value") is not None]
    if not rows:
        return _empty(empty)

    hi = _nice(max([r["value"] for r in rows] + [1.0]))
    slot = (W - PAD_L - PAD_R) / max(1, len(rows))
    bar = min(46.0, slot * 0.62)
    height = H - PAD_T - PAD_B
    body = []
    for index, row in enumerate(rows):
        share = row["value"] / hi
        drawn = max(1.5, height * share)          # zero still shows a baseline
        left = PAD_L + slot * index + (slot - bar) / 2
        tone = row.get("tone") or ""
        style = f'style="fill:var({series_var(index)})"' if not tone else ""
        tip = f'{row["name"]}: {fmt(row["value"], unit)}'
        body.append(f'<rect class="chart-col{(" is-" + tone) if tone else ""}" {style} '
                    f'x="{left:.1f}" y="{PAD_T + height - drawn:.1f}" '
                    f'width="{bar:.1f}" height="{drawn:.1f}" rx="2" '
                    f'data-tip="{_esc(tip)}"/>')
    return _wrap(
        _frame("".join(body), ", ".join(
            f"{r['name']} {fmt(r['value'], unit)}" for r in rows)),
        y_ticks=(fmt(hi, unit), fmt(0.0, unit)),
        x_ticks=[(r["name"], fmt(r["value"], unit)) for r in rows],
        spread=True)


def bullet(value, warn=None, danger=None, ceiling=100.0, unit="%",
           empty="not reporting", label=""):
    """
    One measurement against the thresholds that will act on it.

    A gauge shows a number; a bullet shows the number NEXT TO the line somebody
    wrote down. That is the useful form for cluster saturation, where the
    interesting fact is not "58%" but "58%, and 70% is where it starts buying
    machines".

    `label` names WHICH measurement. Two unlabelled bullets stacked in one card
    are two identical pictures of two different resources, and the reader has
    only their order to go on.
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
    scale = []
    # `kind` is a CSS tone; `word` is what the threshold DOES. "bad at 80%" is
    # the stylesheet talking to itself.
    for threshold, kind, word in ((warn, "warn", "warn"), (danger, "bad", "act")):
        if threshold is None:
            continue
        marks.append(f'<i class="bullet-mark is-{kind}" '
                     f'style="left:{min(100.0, threshold / ceiling * 100):.1f}%"></i>')
        scale.append(f"{word} at {fmt(threshold, unit)}")
    tip = f'{label or "reading"}: {fmt(value, unit)} of {fmt(ceiling, unit)}'
    if scale:
        tip += " · " + ", ".join(scale)
    return (f'<div class="bullet{(" is-" + tone) if tone else ""}" '
            f'data-tip="{_esc(tip)}">'
            f'<span class="bullet-label">{_esc(label)}</span>'
            f'<span class="bullet-track">'
            f'<i class="bullet-fill" style="width:{min(100.0, value / ceiling * 100):.1f}%"></i>'
            f'{"".join(marks)}</span>'
            f'<span class="bullet-value">{_esc(fmt(value, unit))}</span>'
            f'<span class="bullet-scale">0 → {_esc(fmt(ceiling, unit))}'
            f'{(" · " + _esc(", ".join(scale))) if scale else ""}</span>'
            f'</div>')
