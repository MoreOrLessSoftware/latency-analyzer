"""
HTML report for analyze_video.py: one self-contained page with the headline figures, the latency of every press, how
they group, the notes, and the debug images.
"""
import base64
import html
from pathlib import Path

import cv2
import numpy as np


def group_latencies(latencies, stream_fps=None):
    """
    Split the latencies into clusters with gaps between them, which are usually whole stream frames apart.
    Returns (lowest, highest, values) per group, lowest first.
    """
    gap = 1000 / stream_fps / 3 if stream_fps else 2.5
    groups = []
    for value in sorted(latencies):
        if groups and value - groups[-1][-1] <= gap:
            groups[-1].append(value)
        else:
            groups.append([value])
    return groups


def stats_row(name, values):
    values = np.asarray(values)
    if values.size == 0:
        return f"<tr><th>{html.escape(name)}</th><td>0</td>" + "<td>-</td>" * 7 + "</tr>"
    cells = [np.median(values), values.mean(), values.std(), np.percentile(values, 10), np.percentile(values, 90), values.min(), values.max()]
    return f"<tr><th>{html.escape(name)}</th><td>{values.size}</td>" + "".join(f"<td>{v:.2f}</td>" for v in cells) + "</tr>"


def embed_image(path, max_width=None, jpeg=False):
    """The image as a data URI, scaled down to max_width and recompressed as JPEG if asked, or None if missing."""
    image = cv2.imread(str(path)) if Path(path).is_file() else None
    if image is None:
        return None
    if max_width and image.shape[1] > max_width:
        scale = max_width / image.shape[1]
        image = cv2.resize(image, (max_width, int(image.shape[0] * scale)), interpolation=cv2.INTER_AREA)
    ok, data = cv2.imencode(".jpg" if jpeg else ".png", image, [cv2.IMWRITE_JPEG_QUALITY, 85] if jpeg else [])
    if not ok:
        return None
    return f"data:image/{'jpeg' if jpeg else 'png'};base64,{base64.b64encode(data.tobytes()).decode()}"


def dot_plot(presses, stream_fps):
    """An SVG of each press's latency against its number; filled dots where the marker turned white."""
    width, height = 760, 280
    left, right, top, bottom = 44, 64, 14, 34
    top_value = max([p["latency"] for p in presses] + [1.0])
    if stream_fps:
        frame_ms = 1000 / stream_fps
        top_value = frame_ms * (int(top_value / frame_ms) + 1)
    else:
        top_value = 5 * (int(top_value / 5) + 1)
    count = max(len(presses), 2)
    x_of = lambda i: left + (i - 0.5) / count * (width - left - right)
    y_of = lambda v: top + (1 - v / top_value) * (height - top - bottom)

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Latency of each press">']
    # Gridlines: every stream frame when the frame rate is known, otherwise every 5 ms
    step = 1000 / stream_fps if stream_fps else 5
    k = 0
    while k * step <= top_value + 1e-6:
        value = k * step
        y = y_of(value)
        parts.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        label = f"{value:.1f}" if stream_fps else f"{value:.0f}"
        parts.append(f'<text class="tick" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{label}</text>')
        if stream_fps and k > 0:
            parts.append(f'<text class="tick frame" x="{width - right + 6}" y="{y + 4:.1f}">{k} frame{"s" if k > 1 else ""}</text>')
        k += 1
    parts.append(f'<line class="axis" x1="{left}" x2="{width - right}" y1="{y_of(0):.1f}" y2="{y_of(0):.1f}"/>')
    for i in range(1, len(presses) + 1):
        if i == 1 or i % 5 == 0:
            parts.append(f'<text class="tick" x="{x_of(i):.1f}" y="{height - bottom + 16}" text-anchor="middle">{i}</text>')
    parts.append(f'<text class="tick" x="{left}" y="{height - 4}">press</text>')

    for p in presses:
        x, y = x_of(p["input"]), y_of(p["latency"])
        tip = f'Press {p["input"]}: {p["latency"]:.2f} ms, marker {"turned white" if p["rising"] else "turned black"}'
        if stream_fps:
            tip += f', {p["latency"] / (1000 / stream_fps):.2f} frames'
        cls = "dot" if p["rising"] else "dot hollow"
        parts.append(f'<g class="hit" data-tip="{html.escape(tip)}"><rect x="{x - 9:.1f}" y="{top}" width="18" height="{height - top - bottom}" fill="transparent"/>'
                     f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{5 if p["rising"] else 4}"/></g>')
    parts.append("</svg>")
    return "".join(parts)


def nice_step(top_value, max_lines=8):
    """The smallest of 1, 2, 5, 10, 20... ms that splits 0..top_value into at most max_lines gridlines."""
    step = 1.0
    while True:
        for factor in (1, 2, 5):
            if top_value / (step * factor) <= max_lines:
                return step * factor
        step *= 10


def bar_path(x, y_low, y_high, width, radius):
    """An SVG path for a bar from y_low up to y_high (y_high above it), rounded only at its top end."""
    radius = max(0.0, min(radius, width / 2, (y_low - y_high) / 2))
    return (f"M{x:.1f},{y_low:.1f}V{y_high + radius:.1f}Q{x:.1f},{y_high:.1f} {x + radius:.1f},{y_high:.1f}"
            f"H{x + width - radius:.1f}Q{x + width:.1f},{y_high:.1f} {x + width:.1f},{y_high + radius:.1f}V{y_low:.1f}Z")


def stacked_plot(presses, stream_fps):
    """
    An SVG of the time from each key press to the trigger being presented, as a bar from the key press split into
    the marker's logged delay (bottom) and the marker to trigger latency (top). Presses missing from the marker log
    show the latency alone, faded. The top segment is outlined instead of filled where the marker turned black.
    """
    width, height = 760, 300
    left, right, top, bottom = 44, 64, 14, 34
    totals = [p["latency"] + (p.get("marker_ms") or 0) for p in presses]
    logged_totals = [p["latency"] + p["marker_ms"] for p in presses if p.get("marker_ms") is not None]
    highest = max(totals + [1.0])
    step = nice_step(highest)
    top_value = step * (int(highest / step) + 1)
    count = max(len(presses), 2)
    slot = (width - left - right) / count
    bar = max(3.0, min(16.0, slot * 0.6))
    x_of = lambda i: left + (i - 0.5) * slot
    y_of = lambda v: top + (1 - v / top_value) * (height - top - bottom)
    gap = 2  # surface gap between the stacked segments

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Key press to presented for each press">']
    k = 0
    while k * step <= top_value + 1e-6:
        y = y_of(k * step)
        parts.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{k * step:g}</text>')
        k += 1
    for i in range(1, len(presses) + 1):
        if i == 1 or i % 5 == 0:
            parts.append(f'<text class="tick" x="{x_of(i):.1f}" y="{height - bottom + 16}" text-anchor="middle">{i}</text>')
    parts.append(f'<text class="tick" x="{left}" y="{height - 4}">press</text>')

    for p in presses:
        cx = x_of(p["input"])
        x = cx - bar / 2
        frames = f' ({p["latency"] / (1000 / stream_fps):.2f} frames)' if stream_fps else ""
        edge = "marker turned white" if p["rising"] else "marker turned black"
        marker_ms = p.get("marker_ms")
        if marker_ms is not None:
            tip = (f'Press {p["input"]}: key press to presented {p["latency"] + marker_ms:.2f} ms = marker {marker_ms:.2f} '
                   f'+ marker to trigger {p["latency"]:.2f}{frames}, {edge}')
            parts.append(f'<g class="hit" data-tip="{html.escape(tip)}">')
            y0, y1 = y_of(0), y_of(marker_ms)
            if y0 - y1 > 0.5:
                parts.append(f'<rect class="seg client" x="{x:.1f}" y="{y1:.1f}" width="{bar:.1f}" height="{y0 - y1:.1f}"/>')
            low = y1 - gap
        else:
            tip = f'Press {p["input"]}: not in the marker log, marker to trigger {p["latency"]:.2f} ms{frames}, {edge}'
            parts.append(f'<g class="hit unlogged" data-tip="{html.escape(tip)}">')
            low = y_of(0)
        high = y_of(p["latency"] + (marker_ms or 0))
        if p["rising"]:
            parts.append(f'<path class="seg stream" d="{bar_path(x, low, high, bar, 4)}"/>')
        else:
            # Inset by half the stroke so the outline lines up with the filled bars
            parts.append(f'<path class="seg stream hollow" d="{bar_path(x + 1, low - 1, high + 1, bar - 2, 3)}"/>')
        parts.append(f'<rect x="{cx - slot / 2:.1f}" y="{top}" width="{slot:.1f}" height="{height - top - bottom}" fill="transparent"/></g>')

    parts.append(f'<line class="axis" x1="{left}" x2="{width - right}" y1="{y_of(0):.1f}" y2="{y_of(0):.1f}"/>')
    if logged_totals:
        median = float(np.median(logged_totals))
        y = y_of(median)
        parts.append(f'<line class="median" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick median-label" x="{width - right + 6}" y="{y - 2:.1f}">median</text>'
                     f'<text class="tick median-label" x="{width - right + 6}" y="{y + 11:.1f}">{median:.1f} ms</text>')
    parts.append("</svg>")
    return "".join(parts)


STYLE = """
:root {
  color-scheme: light;
  --bg: #f6f6f4; --surface: #fcfcfb; --border: #e2e1dc;
  --text: #0b0b0b; --text-2: #52514e; --muted: #8a8984;
  --series: #2a78d6; --series-2: #eb6834; --grid: #e8e7e3; --warn-bg: #fff6e0; --warn-border: #eda100;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #121211; --surface: #1a1a19; --border: #2e2e2c;
    --text: #f0efec; --text-2: #c3c2b7; --muted: #8f8e86;
    --series: #3987e5; --series-2: #d95926; --grid: #2a2a28; --warn-bg: #2b2412; --warn-border: #c98500;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #121211; --surface: #1a1a19; --border: #2e2e2c;
  --text: #f0efec; --text-2: #c3c2b7; --muted: #8f8e86;
  --series: #3987e5; --series-2: #d95926; --grid: #2a2a28; --warn-bg: #2b2412; --warn-border: #c98500;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, "Segoe UI", sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 2px; word-break: break-all; }
h2 { font-size: 16px; margin: 28px 0 8px; }
.sub { color: var(--text-2); margin: 0 0 20px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.tile .label { color: var(--text-2); font-size: 13px; }
.tile .value { font-size: 28px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .value small { font-size: 15px; font-weight: 400; color: var(--text-2); }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { padding: 4px 10px; text-align: right; white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
thead th { color: var(--text-2); font-weight: 500; border-bottom: 1px solid var(--border); }
tbody th { font-weight: 500; }
.legend { color: var(--text-2); font-size: 13px; margin: 4px 0 0; }
.legend svg { vertical-align: -2px; }
svg.chart { width: 100%; height: auto; display: block; }
svg .grid { stroke: var(--grid); stroke-width: 1; }
svg .axis { stroke: var(--muted); stroke-width: 1; }
svg .tick { fill: var(--text-2); font-size: 11px; }
svg .tick.frame { fill: var(--muted); }
svg .dot { fill: var(--series); }
svg .dot.hollow { fill: var(--surface); stroke: var(--series); stroke-width: 2; }
svg .hit:hover .dot { stroke: var(--text); stroke-width: 2; }
svg .dots circle { fill: var(--series); fill-opacity: 0.6; }
svg .seg.client { fill: var(--series-2); }
svg .seg.stream { fill: var(--series); }
svg .seg.stream.hollow { fill: var(--surface); stroke: var(--series); stroke-width: 2; }
svg .hit.unlogged .seg { opacity: 0.45; }
svg .hit:hover .seg { filter: brightness(1.15); }
svg .median { stroke: var(--text-2); stroke-width: 1.5; stroke-dasharray: 5 4; }
svg .median-label { fill: var(--text); }
#tip { position: fixed; pointer-events: none; background: var(--text); color: var(--bg); padding: 4px 8px; border-radius: 4px;
       font-size: 13px; display: none; white-space: nowrap; z-index: 10; }
.note { background: var(--warn-bg); border-left: 3px solid var(--warn-border); padding: 8px 12px; margin: 8px 0; border-radius: 4px; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 16px; margin: 0; }
dt { color: var(--text-2); }
dd { margin: 0; }
details summary { cursor: pointer; color: var(--text-2); }
.scroll { overflow-x: auto; }
.scroll img { max-width: none; height: 200px; }
img { max-width: 100%; border-radius: 4px; }
.files { color: var(--text-2); font-size: 13px; }
"""

SCRIPT = """
const tip = document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el => {
  el.addEventListener('mousemove', e => {
    tip.textContent = el.dataset.tip;
    tip.style.display = 'block';
    const x = Math.min(e.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = x + 'px';
    tip.style.top = (e.clientY - 34) + 'px';
  });
  el.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
});
"""


def write_report(path, info, presses, notes, files):
    """
    Write the report.

    info: dict of video, frames, fps, fps_source, stream, stream_fps, found, estimated, marker, same_rows,
      flip counts and edge lengths. presses: one dict per paired press with input, latency, rising, host_rising and
      seq. notes: plain text notes. files: dict with the csv, regions and trace paths.
    """
    stream_fps = info.get("stream_fps")
    latencies = [p["latency"] for p in presses]
    # With Moonlight's marker log: the marker's own delay, and the key press to the trigger being presented
    logged = [p for p in presses if p.get("marker_ms") is not None]
    marker_delays = [p["marker_ms"] for p in logged]
    press_to_present = [p["latency"] + p["marker_ms"] for p in logged]
    esc = html.escape
    out = []
    out.append(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
               f'<title>Latency {esc(Path(info["video"]).stem)}</title><style>{STYLE}</style></head><body><main>')
    out.append(f'<h1>{esc(Path(info["video"]).name)}</h1>')
    stream_text = f'{info["stream"][0]}x{info["stream"][1]}' + (f' at {stream_fps:g} fps' if stream_fps else "")
    out.append(f'<p class="sub">Stream {stream_text} &middot; camera {info["fps"]:g} fps, {info["frames"] / info["fps"]:.1f} s</p>')

    # Headline tiles
    tiles = []
    if press_to_present:
        tiles.append(("Key press to presented, median", f'{np.median(press_to_present):.2f} <small>ms</small>'))
    if latencies:
        values = np.asarray(latencies)
        tiles.append(("Median latency" if not press_to_present else "Marker to trigger, median", f'{np.median(values):.2f} <small>ms</small>'))
        tiles.append(("Mean", f'{values.mean():.2f} <small>ms</small>'))
        tiles.append(("Spread (p10 to p90)", f'{np.percentile(values, 10):.1f}&ndash;{np.percentile(values, 90):.1f} <small>ms</small>'))
    tiles.append(("Presses paired", f'{len(presses)} <small>of {info["marker_flips"]}</small>'))
    out.append('<div class="tiles">' + "".join(f'<div class="tile"><div class="label">{l}</div><div class="value">{v}</div></div>' for l, v in tiles) + "</div>")

    if notes:
        out.append("<h2>Notes</h2>")
        out.extend(f'<div class="note">{esc(n)}</div>' for n in notes)

    if presses:
        if logged:
            unlogged = len(presses) - len(logged)
            out.append("<h2>Key press to presented, every press, ms</h2>")
            out.append(f'<div class="card">{stacked_plot(presses, stream_fps)}'
                       '<p class="legend"><svg width="12" height="12"><rect width="12" height="12" rx="2" fill="var(--series-2)"/></svg> '
                       'key press to marker presented (marker log) &nbsp; '
                       '<svg width="12" height="12"><rect width="12" height="12" rx="2" fill="var(--series)"/></svg> '
                       'marker to trigger &nbsp; '
                       '<svg width="12" height="12"><rect x="1" y="1" width="10" height="10" rx="2" fill="none" stroke="var(--series)" stroke-width="2"/></svg> '
                       'marker turned black &nbsp; dashed: median key press to presented'
                       + (f' &nbsp; faded: {unlogged} press{"es" if unlogged != 1 else ""} not in the marker log, marker to trigger only' if unlogged else "")
                       + "</p></div>")
            out.append("<h2>Marker to trigger, every press, ms</h2>")
        else:
            out.append("<h2>Latency of every press, ms</h2>")
        out.append(f'<div class="card">{dot_plot(presses, stream_fps)}'
                   '<p class="legend"><svg width="12" height="12"><circle cx="6" cy="6" r="5" fill="var(--series)"/></svg> marker turned white &nbsp; '
                   '<svg width="12" height="12"><circle cx="6" cy="6" r="4" fill="none" stroke="var(--series)" stroke-width="2"/></svg> marker turned black'
                   + ("" if stream_fps else " &nbsp; (pass --stream-fps for frame gridlines)") + "</p></div>")

        out.append("<h2>Groups</h2>")
        out.append('<div class="card"><table><thead><tr><th>Group</th><th>Presses</th><th>Share</th><th>Range ms</th><th>Mean ms</th>'
                   + ("<th>Frames</th>" if stream_fps else "") + "</tr></thead><tbody>")
        for group in group_latencies(latencies, stream_fps):
            mean = float(np.mean(group))
            frames = f"<td>{mean / (1000 / stream_fps):.2f}</td>" if stream_fps else ""
            frame_count = round(mean / (1000 / stream_fps)) if stream_fps else 0
            name = f"{frame_count} frame{'' if frame_count == 1 else 's'}" if stream_fps else f"~{mean:.0f} ms"
            out.append(f"<tr><th>{name}</th><td>{len(group)}</td><td>{len(group) / len(latencies):.0%}</td>"
                       f"<td>{min(group):.2f}&ndash;{max(group):.2f}</td><td>{mean:.2f}</td>{frames}</tr>")
        out.append("</tbody></table></div>")

        out.append("<h2>Statistics</h2>")
        out.append('<div class="card"><table><thead><tr><th>ms</th><th>n</th><th>median</th><th>mean</th><th>sd</th><th>p10</th><th>p90</th><th>min</th><th>max</th></tr></thead><tbody>')
        out.append(stats_row("all", latencies))
        if logged:
            out.append(stats_row("marker delay (marker log)", marker_delays))
            out.append(stats_row("key press to presented", press_to_present))
        out.append(stats_row("marker turned white", [p["latency"] for p in presses if p["rising"]]))
        out.append(stats_row("marker turned black", [p["latency"] for p in presses if not p["rising"]]))
        opposite = [p["latency"] for p in presses if p["rising"] != p["host_rising"]]
        if opposite:
            out.append(stats_row("same edge", [p["latency"] for p in presses if p["rising"] == p["host_rising"]]))
            out.append(stats_row("opposite edge", opposite))
        out.append("</tbody></table></div>")

    out.append("<h2>Measurement</h2>")
    marker = info["marker"]
    rows = [
        ("Video", f'{info["frames"]} frames at {info["fps"]:g} fps ({esc(info["fps_source"])})'),
        ("Bullseyes", f'found in {info["found"]}/{info["frames"]} frames' + (f', {info["estimated"]} with one hidden and estimated' if info["estimated"] else "")),
        ("Marker", f'x {marker.x:.0f} y {marker.y:.0f}, {marker.w:.0f}x{marker.h:.0f} stream pixels; '
                   + ("sampled on the trigger rows" if info["same_rows"] else "<b>not</b> on the trigger rows, so scanout timing differs")),
        ("Flips", f'marker {info["marker_flips"]}, trigger {info["trigger_flips"]}, paired {len(presses)}, '
                  f'unanswered {info["missed"]}, unpaired trigger {info["extra"]}'),
        ("Edges, 10&ndash;90 %", f'marker rise {info["ramps"][0]}, fall {info["ramps"][1]}; trigger rise {info["ramps"][2]}, fall {info["ramps"][3]}'),
    ]
    if info.get("marker_log_searched") and not info.get("marker_log"):
        rows.append(("Marker log", f'none matching found in {esc(str(info["marker_log_searched"]))}'))
    if info.get("marker_log"):
        rows.append(("Marker log", f'{esc(Path(info["marker_log"]).name)}: {info["logged_flips"]} presses logged, '
                                      f'{len(logged)} of {len(presses)} presses matched'))
    out.append('<div class="card"><dl>' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl></div>")
    if logged:
        out.append('<p class="legend">The latency above runs from the marker appearing to the trigger appearing. The marker '
                   'only appears with the next frame the client draws, so Moonlight logs how long after the key press that '
                   'frame was presented. Adding the two gives the key press to the trigger being presented. Neither '
                   'includes the delay of the display itself.</p>')

    if presses:
        out.append('<h2>Per press</h2><div class="card"><details><summary>Show all presses</summary><table><thead><tr><th>Press</th><th>Latency ms</th>'
                   + ("<th>Frames</th>" if stream_fps else "")
                   + ("<th>Marker delay ms</th><th>Press to presented ms</th>" if logged else "")
                   + "<th>Marker</th><th>Trigger</th><th>Host input #</th></tr></thead><tbody>")
        for p in presses:
            frames = f'<td>{p["latency"] / (1000 / stream_fps):.2f}</td>' if stream_fps else ""
            delay = ""
            if logged:
                delay = (f'<td>{p["marker_ms"]:.2f}</td><td>{p["latency"] + p["marker_ms"]:.2f}</td>' if p.get("marker_ms") is not None
                         else "<td>-</td><td>-</td>")
            out.append(f'<tr><td>{p["input"]}</td><td>{p["latency"]:.2f}</td>{frames}{delay}<td>{"rise" if p["rising"] else "fall"}</td>'
                       f'<td>{"rise" if p["host_rising"] else "fall"}</td><td>{p["seq"]}</td></tr>')
        out.append("</tbody></table></details></div>")

    trace = embed_image(files["trace"], max_width=4000)
    regions = embed_image(files["regions"], max_width=1600, jpeg=True)
    if trace:
        out.append('<h2>Brightness trace</h2><div class="card"><p class="legend">Green: the marker on the client. Blue: the trigger in the stream. '
                   'Grey lines are detected flips and the green links join each pair. Scroll sideways.</p>'
                   f'<div class="scroll"><img src="{trace}" alt="Brightness of both squares over the video"></div></div>')
    if regions:
        out.append('<h2>Sampled regions</h2><div class="card"><p class="legend">Check that the boxes sit on the squares.</p>'
                   f'<img src="{regions}" alt="Camera frame with the sampled regions outlined"></div>')

    out.append(f'<p class="files">Files: {esc(Path(files["csv"]).name)}, {esc(Path(files["trace"]).name)}, {esc(Path(files["regions"]).name)} '
               f'in {esc(str(Path(files["csv"]).parent))}</p>')
    out.append(f'<div id="tip"></div></main><script>{SCRIPT}</script></body></html>')
    Path(path).write_text("\n".join(out), encoding="utf-8")


def time_scatter(points, max_points=4000):
    """An SVG of (seconds, ms) points: each frame's present to photon time over the recording."""
    width, height = 760, 260
    left, right, top, bottom = 44, 20, 14, 34
    if len(points) > max_points:
        points = points[:: int(np.ceil(len(points) / max_points))]
    xs = np.array([p[0] for p in points])
    ys = np.array([p[1] for p in points])
    span = max(float(xs.max() - xs.min()), 1e-3) if xs.size else 1.0
    x0 = float(xs.min()) if xs.size else 0.0
    highest = float(np.percentile(ys, 99.5)) if ys.size else 1.0
    step = nice_step(max(highest, 1.0))
    top_value = step * (int(max(highest, 1.0) / step) + 1)
    x_of = lambda t: left + (t - x0) / span * (width - left - right)
    y_of = lambda v: top + (1 - min(v, top_value) / top_value) * (height - top - bottom)

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Present to photon of every frame over time">']
    k = 0
    while k * step <= top_value + 1e-6:
        y = y_of(k * step)
        parts.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{k * step:g}</text>')
        k += 1
    second_step = nice_step(span, 10)
    s = 0.0
    while s <= span + 1e-9:
        parts.append(f'<text class="tick" x="{x_of(x0 + s):.1f}" y="{height - bottom + 16}" text-anchor="middle">{s:g}</text>')
        s += second_step
    parts.append(f'<text class="tick" x="{width - right}" y="{height - 4}" text-anchor="end">seconds</text>')
    parts.append(f'<line class="axis" x1="{left}" x2="{width - right}" y1="{y_of(0):.1f}" y2="{y_of(0):.1f}"/>')
    parts.append('<g class="dots">' + "".join(f'<circle cx="{x_of(t):.1f}" cy="{y_of(v):.1f}" r="1.6"/>' for t, v in zip(xs, ys)) + "</g>")
    if ys.size:
        y = y_of(float(np.median(ys)))
        parts.append(f'<line class="median" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
    parts.append("</svg>")
    return "".join(parts)


def histogram(values, bin_ms=0.5, label="ms"):
    """An SVG histogram of values in ms, centred on zero, with a tooltip per bar."""
    width, height = 760, 220
    left, right, top, bottom = 44, 20, 14, 34
    values = np.asarray(values)
    reach = max(5.0, float(np.percentile(np.abs(values), 99.5)) if values.size else 5.0)
    reach = bin_ms * np.ceil(reach / bin_ms)
    edges = np.arange(-reach, reach + bin_ms / 2, bin_ms)
    counts, _ = np.histogram(np.clip(values, -reach, reach - 1e-9), edges)
    highest = max(int(counts.max()) if counts.size else 1, 1)
    x_of = lambda v: left + (v + reach) / (2 * reach) * (width - left - right)
    y_of = lambda c: top + (1 - c / highest) * (height - top - bottom)
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Histogram">']
    step = nice_step(reach, 5)
    v = -step * np.floor(reach / step)
    while v <= reach + 1e-9:
        x = x_of(v)
        parts.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{height - bottom}"/>')
        parts.append(f'<text class="tick" x="{x:.1f}" y="{height - bottom + 16}" text-anchor="middle">{v:+g}</text>')
        v += step
    parts.append(f'<text class="tick" x="{width - right}" y="{height - 4}" text-anchor="end">{html.escape(label)}</text>')
    bar = (width - left - right) / len(counts)
    total = max(values.size, 1)
    for i, c in enumerate(counts):
        if c == 0:
            continue
        x = left + i * bar
        y = y_of(c)
        tip = f"{edges[i]:+.1f} to {edges[i + 1]:+.1f} ms: {c} ({c / total:.1%})"
        parts.append(f'<g class="hit" data-tip="{html.escape(tip)}"><rect class="seg stream" x="{x + 0.5:.1f}" y="{y:.1f}" '
                     f'width="{max(bar - 1, 1):.1f}" height="{y_of(0) - y:.1f}"/></g>')
    parts.append(f'<line class="axis" x1="{left}" x2="{width - right}" y1="{y_of(0):.1f}" y2="{y_of(0):.1f}"/>')
    parts.append("</svg>")
    return "".join(parts)


def write_camera_report(path, info, flips, frames, pacing, notes, files):
    """
    Write the report for a Latency Cam video.

    info: video, frames, fps, stream, stream_fps, found, estimated, fit (ClockFit), flips_seen, flips_logged, device.
    flips: dicts with seq, source, latency, rising, edge_ms. frames: dicts with frame, photon_s, latency.
    pacing: camera_timing.Pacing. notes: plain text. files: dict with the flips, frames and regions paths.
    """
    esc = html.escape
    stream_fps = info.get("stream_fps")
    fit = info["fit"]
    flip_latencies = [f["latency"] for f in flips]
    frame_latencies = [f["latency"] for f in frames]
    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
           f'<title>Latency {esc(Path(info["video"]).stem)}</title><style>{STYLE}</style></head><body><main>']
    out.append(f'<h1>{esc(Path(info["video"]).name)}</h1>')
    stream_text = f'{info["stream"][0]}x{info["stream"][1]}' + (f' at {stream_fps:g} fps' if stream_fps else "")
    out.append(f'<p class="sub">Stream {stream_text} &middot; Latency Cam, {esc(info["device"])}, {info["fps"]:g} fps, '
               f'{info["frames"] / info["fps"]:.1f} s</p>')

    tiles = []
    if flip_latencies:
        tiles.append(("Present to photon, flips, median", f'{np.median(flip_latencies):.2f} <small>ms</small>'))
    if frame_latencies:
        tiles.append(("Present to photon, every frame, median", f'{np.median(frame_latencies):.2f} <small>ms</small>'))
    if pacing.mismatch_ms.size:
        tiles.append(("Pacing, client &minus; host interval, sd", f'{pacing.mismatch_ms.std():.2f} <small>ms</small>'))
        tiles.append(("Uneven frame intervals", f'{pacing.uneven / pacing.mismatch_ms.size:.1%} <small>of {pacing.mismatch_ms.size}</small>'))
    if pacing.frames_presented:
        tiles.append(("Frames not seen", f'{pacing.not_seen} <small>of {pacing.frames_presented}</small>'))
    out.append('<div class="tiles">' + "".join(f'<div class="tile"><div class="label">{l}</div><div class="value">{v}</div></div>' for l, v in tiles) + "</div>")

    if notes:
        out.append("<h2>Notes</h2>")
        out.extend(f'<div class="note">{esc(n)}</div>' for n in notes)

    if flips:
        presses = [{"input": i + 1, "latency": f["latency"], "rising": f["rising"]} for i, f in enumerate(flips)]
        out.append("<h2>Present to photon, every trigger flip, ms</h2>")
        out.append(f'<div class="card">{dot_plot(presses, stream_fps)}'
                   '<p class="legend">From the host presenting the flip to the trigger crossing halfway on the client display. '
                   '<svg width="12" height="12"><circle cx="6" cy="6" r="5" fill="var(--series)"/></svg> turned white &nbsp; '
                   '<svg width="12" height="12"><circle cx="6" cy="6" r="4" fill="none" stroke="var(--series)" stroke-width="2"/></svg> turned black'
                   + ("" if stream_fps else " &nbsp; (pass --stream-fps for frame gridlines)") + "</p></div>")

    if frames:
        zero = frames[0]["photon_s"]
        out.append("<h2>Present to photon, every frame, ms</h2>")
        out.append(f'<div class="card">{time_scatter([(f["photon_s"] - zero, f["latency"]) for f in frames])}'
                   '<p class="legend">Each dot is one host frame, timed by the frame number patch that changed with it. '
                   'Dashed: median.</p></div>')

    if pacing.mismatch_ms.size:
        out.append("<h2>Pacing: client interval &minus; host interval, ms</h2>")
        out.append(f'<div class="card">{histogram(pacing.mismatch_ms, label="client minus host, ms")}'
                   '<p class="legend">For each pair of consecutive frames, how much longer (right) or shorter (left) the client '
                   'showed the first than the host took between presenting them. Even pacing piles up at 0; a display at a '
                   'fixed refresh rate spreads out by its refresh interval.</p></div>')

    out.append("<h2>Statistics</h2>")
    out.append('<div class="card"><table><thead><tr><th>ms</th><th>n</th><th>median</th><th>mean</th><th>sd</th><th>p10</th><th>p90</th><th>min</th><th>max</th></tr></thead><tbody>')
    out.append(stats_row("present to photon, flips", flip_latencies))
    out.append(stats_row("present to photon, flips turning white", [f["latency"] for f in flips if f["rising"]]))
    out.append(stats_row("present to photon, flips turning black", [f["latency"] for f in flips if not f["rising"]]))
    out.append(stats_row("present to photon, every frame", frame_latencies))
    out.append(stats_row("host present interval", pacing.host_intervals_ms))
    out.append(stats_row("client display interval", pacing.client_intervals_ms))
    out.append(stats_row("client minus host interval", pacing.mismatch_ms))
    out.append("</tbody></table></div>")

    out.append("<h2>Measurement</h2>")
    rows = [
        ("Video", f'{info["frames"]} frames at {info["fps"]:g} fps, each timed by its sensor timestamp and rolling shutter'),
        ("Bullseyes", f'found in {info["found"]}/{info["frames"]} frames' + (f', {info["estimated"]} with one hidden and estimated' if info["estimated"] else "")),
        ("Clock sync", f'{fit.windows} of {fit.samples} exchanges used; fastest round trip {fit.min_rtt_ms:.2f} ms '
                       f'(median {fit.median_rtt_ms:.2f}); scatter {fit.residual_ms:.3f} ms; phone clock {fit.drift_ppm:+.1f} ppm against the host&#39;s'),
        ("Flips", f'{info["flips_seen"]} seen, {len(flips)} matched to the host log ({info["flips_logged"]} logged)'),
        ("Frames", f'{pacing.frames_seen} seen of {pacing.frames_presented} presented while filming; '
                   f'{pacing.mismatch_ms.size} consecutive pairs'),
    ]
    out.append('<div class="card"><dl>' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl></div>")
    out.append('<p class="legend">Present to photon runs from the host&#39;s present call to the client display showing the '
               'change, so it includes the capture wait, encoding, the network, decoding, the client&#39;s pacing and the '
               'display&#39;s own delay.</p>')

    regions = embed_image(files["regions"], max_width=1600, jpeg=True)
    if regions:
        out.append('<h2>Sampled regions</h2><div class="card"><p class="legend">Check that the boxes sit on the trigger and the frame number patches.</p>'
                   f'<img src="{regions}" alt="Camera frame with the sampled regions outlined"></div>')
    out.append(f'<p class="files">Files: {esc(Path(files["flips"]).name)}, {esc(Path(files["frames"]).name)}, '
               f'{esc(Path(files["regions"]).name)} in {esc(str(Path(files["flips"]).parent))}</p>')
    out.append(f'<div id="tip"></div></main><script>{SCRIPT}</script></body></html>')
    Path(path).write_text("\n".join(out), encoding="utf-8")
