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


STYLE = """
:root {
  color-scheme: light;
  --bg: #f6f6f4; --surface: #fcfcfb; --border: #e2e1dc;
  --text: #0b0b0b; --text-2: #52514e; --muted: #8a8984;
  --series: #2a78d6; --grid: #e8e7e3; --warn-bg: #fff6e0; --warn-border: #eda100;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #121211; --surface: #1a1a19; --border: #2e2e2c;
    --text: #f0efec; --text-2: #c3c2b7; --muted: #8f8e86;
    --series: #3987e5; --grid: #2a2a28; --warn-bg: #2b2412; --warn-border: #c98500;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #121211; --surface: #1a1a19; --border: #2e2e2c;
  --text: #f0efec; --text-2: #c3c2b7; --muted: #8f8e86;
  --series: #3987e5; --grid: #2a2a28; --warn-bg: #2b2412; --warn-border: #c98500;
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
