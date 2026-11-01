"""
HTML report for a Latency Cam video: one self-contained page with the headline figures, every trigger flip and
every frame's latency, the pacing, the pipeline from Sunshine's and Moonlight's logs, and the sampled regions.
"""
import base64
import html
from pathlib import Path

import cv2
import numpy as np

from traces import CAPTURE_TO_CLIENT_PRESENT


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
    """
    An SVG of each trigger flip's latency against its number; filled dots where the trigger turned white. presses:
    dicts with input (the flip's number), latency (ms) and rising.
    """
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

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Latency of each trigger flip">']
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
    parts.append(f'<text class="tick" x="{left}" y="{height - 4}">flip</text>')

    for p in presses:
        x, y = x_of(p["input"]), y_of(p["latency"])
        tip = f'Flip {p["input"]}: {p["latency"]:.2f} ms, trigger {"turned white" if p["rising"] else "turned black"}'
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
svg .dots circle.fall { fill: var(--series-2); }
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


def time_scatter(points, max_points=4000):
    """
    An SVG of (seconds, ms) or (seconds, ms, turned white) points: each frame's present to photon time over the
    recording, coloured by the direction its patch changed when that is given.
    """
    width, height = 760, 260
    left, right, top, bottom = 44, 20, 14, 34
    if len(points) > max_points:
        points = points[:: int(np.ceil(len(points) / max_points))]
    xs = np.array([p[0] for p in points])
    ys = np.array([p[1] for p in points])
    falls = [len(p) > 2 and not p[2] for p in points]
    span = max(float(xs.max() - xs.min()), 1e-3) if xs.size else 1.0
    x0 = float(xs.min()) if xs.size else 0.0
    highest = float(np.percentile(ys, 99.5)) if ys.size else 1.0
    step = nice_step(max(highest, 1.0))
    top_value = step * (int(max(highest, 1.0) / step) + 1)
    x_of = lambda t: left + (t - x0) / span * (width - left - right)
    y_of = lambda v: top + (1 - min(v, top_value) / top_value) * (height - top - bottom)

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Host present to photon of every frame over time">']
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
    parts.append('<g class="dots">' + "".join(f'<circle{" class=\"fall\"" if fall else ""} cx="{x_of(t):.1f}" cy="{y_of(v):.1f}" r="1.6"/>'
                                              for t, v, fall in zip(xs, ys, falls)) + "</g>")
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

    # "Host present" is the game (the test app) handing its frame to Windows on the host, "client present" Moonlight
    # handing the decoded frame to Windows on the client, so the names line up with Moonlight's "capture to present"
    pipeline = info.get("pipeline")
    medians = {s.label: float(np.median(s.values)) for s in pipeline.stages} if pipeline else {}

    def by_edge(label):
        """'white / black' medians of a stage measured for each edge, or None."""
        white, black = medians.get(f"{label}, to white"), medians.get(f"{label}, to black")
        if white is None or black is None:
            return None
        return f'{white:.2f} <small>/ {black:.2f} ms</small>'

    tiles = []
    if flip_latencies:
        tiles.append(("Host present &rarr; photon, flips, median", f'{np.median(flip_latencies):.2f} <small>ms</small>'))
    if frame_latencies:
        tiles.append(("Host present &rarr; photon, every frame, median", f'{np.median(frame_latencies):.2f} <small>ms</small>'))
    if by_edge("capture -> photon"):
        tiles.append(("Capture &rarr; photon, to white / black", by_edge("capture -> photon")))
    if CAPTURE_TO_CLIENT_PRESENT in medians:
        tiles.append(("Capture &rarr; client present (Moonlight&#39;s capture to present)",
                      f'{medians[CAPTURE_TO_CLIENT_PRESENT]:.2f} <small>ms</small>'))
    if by_edge("client present -> photon"):
        tiles.append(("Client present &rarr; photon, to white / black", by_edge("client present -> photon")))
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
        out.append("<h2>Host present &rarr; photon, every trigger flip, ms</h2>")
        out.append(f'<div class="card">{dot_plot(presses, stream_fps)}'
                   '<p class="legend">From the host presenting the flip to the trigger crossing halfway on the client display. '
                   '<svg width="12" height="12"><circle cx="6" cy="6" r="5" fill="var(--series)"/></svg> turned white &nbsp; '
                   '<svg width="12" height="12"><circle cx="6" cy="6" r="4" fill="none" stroke="var(--series)" stroke-width="2"/></svg> turned black'
                   + ("" if stream_fps else " &nbsp; (pass --stream-fps for frame gridlines)") + "</p></div>")

    if frames:
        zero = frames[0]["photon_s"]
        out.append("<h2>Host present &rarr; photon, every frame, ms</h2>")
        out.append(f'<div class="card">{time_scatter([(f["photon_s"] - zero, f["latency"], f.get("rising", True)) for f in frames])}'
                   '<p class="legend">Each dot is one host frame, timed by the frame number patch that changed with it. '
                   '<svg width="12" height="12"><circle cx="6" cy="6" r="5" fill="var(--series)"/></svg> patch turned white &nbsp; '
                   '<svg width="12" height="12"><circle cx="6" cy="6" r="5" fill="var(--series-2)"/></svg> patch turned black &nbsp; '
                   'Dashed: median.</p></div>')

    if pacing.mismatch_ms.size:
        edge_difference = info.get("edge_difference", 0.0)
        out.append("<h2>Pacing: client interval &minus; host interval, ms</h2>")
        out.append(f'<div class="card">{histogram(pacing.mismatch_ms, label="client minus host, ms")}'
                   '<p class="legend">For each pair of consecutive frames, how much longer (right) or shorter (left) the client '
                   'showed the first than the host took between presenting them. Even pacing piles up at 0; a display at a '
                   'fixed refresh rate spreads out by its refresh interval. Client times are corrected for each kind of '
                   f'edge reaching halfway at its own delay (here, going to black {edge_difference:+.2f} ms against going '
                   'to white), so the display&#39;s rise and fall times don&#39;t count as uneven pacing.</p></div>')

    out.append("<h2>Statistics</h2>")
    out.append('<div class="card"><table><thead><tr><th>ms</th><th>n</th><th>median</th><th>mean</th><th>sd</th><th>p10</th><th>p90</th><th>min</th><th>max</th></tr></thead><tbody>')
    out.append(stats_row("host present → photon, flips", flip_latencies))
    out.append(stats_row("host present → photon, flips turning white", [f["latency"] for f in flips if f["rising"]]))
    out.append(stats_row("host present → photon, flips turning black", [f["latency"] for f in flips if not f["rising"]]))
    out.append(stats_row("host present → photon, every frame", frame_latencies))
    out.append(stats_row("host present → photon, frames turning white", [f["latency"] for f in frames if f.get("rising", True)]))
    out.append(stats_row("host present → photon, frames turning black", [f["latency"] for f in frames if not f.get("rising", True)]))
    out.append(stats_row("host present interval", pacing.host_intervals_ms))
    out.append(stats_row("client display interval (corrected)", pacing.client_intervals_ms))
    out.append(stats_row("client minus host interval", pacing.mismatch_ms))
    out.append("</tbody></table></div>")

    if pipeline and pipeline.stages:
        logs = [name for name in (pipeline.sunshine_file, pipeline.moonlight_file) if name]
        out.append("<h2>Pipeline, from " + ("Sunshine&#39;s and Moonlight&#39;s traces" if len(logs) == 2 else
                                            "Sunshine&#39;s frame log" if pipeline.sunshine_file else "Moonlight&#39;s pacing trace") + "</h2>")
        legend = (f'{pipeline.joined} of {pipeline.camera_frames} frames followed from the test app through '
                  f'{" and ".join(esc(name) for name in logs)}. ')
        if pipeline.moonlight_file:
            legend += ('Client stages are on Moonlight&#39;s clock, which is offset from the host&#39;s by an unknown amount, '
                       'so no stage spans both except through the camera. ')
        if not pipeline.sunshine_file:
            legend += ('Without Sunshine&#39;s frame log, Moonlight&#39;s rows were matched to the test app&#39;s frames by '
                       'the pattern of their timing, and the host&#39;s stages are missing. ')
        if not pipeline.moonlight_file:
            legend += 'Add Moonlight&#39;s pacing trace for the client&#39;s stages. '
        out.append(f'<p class="legend">{legend}</p>')
        out.append('<div class="card"><table><thead><tr><th>Stage</th><th>Source</th><th>n</th><th>median</th><th>sd</th>'
                   '<th>p10</th><th>p90</th></tr></thead><tbody>')
        for stage in pipeline.stages:
            st = stage.stats()
            out.append(f'<tr><th>{esc(st["label"]).replace("-&gt;", "&rarr;")}</th><td>{esc(st["source"])}</td><td>{st["n"]}</td><td>{st["median"]:.2f}</td>'
                       f'<td>{st["sd"]:.2f}</td><td>{st["p10"]:.2f}</td><td>{st["p90"]:.2f}</td></tr>')
        out.append("</tbody></table></div>")
        checks = pipeline.checks
        lines = []
        if "moonlight_capture_to_present_sd_ms" in checks:
            lines.append(f"Client present minus capture varied by {checks['moonlight_capture_to_present_sd_ms']:.2f} ms "
                         "(sd, so the clocks' offset doesn't matter): how steady the whole path from capture to the "
                         "client present was.")
        for direction in ("white", "black"):
            key = f"photon_minus_present_sd_to_{direction}_ms"
            if key in checks:
                lines.append(f"Photon (camera) minus client present (Moonlight) varied by {checks[key]:.2f} ms for frames "
                             f"turning {direction}: how closely the camera and Moonlight agree on each frame&#39;s timing.")
        if lines:
            out.append('<p class="legend">' + "<br>".join(lines) + "</p>")

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
    legend = ('Host present &rarr; photon runs from the game (the test app) handing its frame to Windows on the host '
              'to the client display showing the change: the capture wait, encoding, the network, decoding, the '
              'client&#39;s pacing and the client display&#39;s own delay.')
    if pipeline and pipeline.moonlight_file:
        legend += (' Moonlight&#39;s &ldquo;capture to present&rdquo; is the middle of that, from capture to Moonlight '
                   'handing the frame to Windows on the client (the client present).')
    out.append(f'<p class="legend">{legend}</p>')

    regions = embed_image(files["regions"], max_width=1600, jpeg=True)
    if regions:
        out.append('<h2>Sampled regions</h2><div class="card"><p class="legend">Check that the boxes sit on the trigger and the frame number patches.</p>'
                   f'<img src="{regions}" alt="Camera frame with the sampled regions outlined"></div>')
    out.append(f'<p class="files">Files: {esc(Path(files["flips"]).name)}, {esc(Path(files["frames"]).name)}, '
               f'{esc(Path(files["regions"]).name)}</p>')
    out.append(f'<div id="tip"></div></main><script>{SCRIPT}</script></body></html>')
    Path(path).write_text("\n".join(out), encoding="utf-8")
