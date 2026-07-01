"""previewer.html - wrap the scene dict in a single, self-contained three.js viewer page.

``render_html`` injects ``build_scene``'s dict (as JSON) into a static template: one double-
clickable ``.html`` that pulls three.js from a CDN and draws the layout. The camera orbits AND
pans (right-drag / arrow keys), and a layer-by-layer slider isolates each y-level. Machines are
solid boxes with their name on the front face (placeholder until real textures); cables/pipes are
square bars sized by thickness, with a short lead connecting each route to the machine face it
docks on; auto-output is a chunky arrow. A side panel lists the machine/route legend plus the
system's boundary inputs, outputs, and power (``scene.io``), with a per-tick / per-second rate
toggle. The view frames the layout's *actual* extent (``scene.bounds``), not the solver's
oversized search region.

The scene JSON is *inlined*, not fetched, so there is no ``file://`` CORS problem. The template is
assembled by replacing a single ``__SCENE_JSON__`` token (NOT an f-string / ``.format``) so the
JS/CSS braces stay literal. Vendoring three.js to drop the CDN (offline) is a noted follow-up.
"""

from __future__ import annotations

import json
from typing import Any

_THREE = "https://unpkg.com/three@0.160.0"
_SCENE_TOKEN = "__SCENE_JSON__"  # a template placeholder the scene JSON replaces

_TEMPLATE = (
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gtnh-solve preview</title>
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; background: #1a1d22;
               font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; color: #e8eaed; }
  #hud, #legend, #controls { position: fixed; z-index: 10; background: rgba(20,22,28,0.82);
               border: 1px solid #333a44; border-radius: 6px; padding: 8px 10px; }
  #hud { top: 10px; left: 10px; }
  #hint { color: #8b94a0; margin-top: 4px; }
  #legend { top: 10px; right: 10px; max-height: 80vh; overflow: auto; }
  #controls { bottom: 10px; left: 10px; display: flex; gap: 12px; align-items: center; }
  #controls input[type=range] { width: 180px; }
  .sw { display: inline-block; width: 11px; height: 11px; margin-right: 6px; border-radius: 2px;
        vertical-align: middle; }
  b { color: #aab2bd; font-weight: 600; }
  button { font: inherit; color: #e8eaed; background: #2a2f37; border: 1px solid #3a4150;
           border-radius: 4px; padding: 3px 8px; cursor: pointer; }
</style>
</head>
<body>
<div id="hud">loading...<div id="hint">drag: rotate &middot; right-drag / arrows: pan &middot; scroll: zoom</div></div>
<div id="legend"></div>
<div id="controls">
  <span>layer <b id="layerVal">all</b></span>
  <input id="layer" type="range" min="-1" max="0" value="-1" step="1">
  <button id="reset">reset camera</button>
  <button id="rateUnit" title="toggle throughput units">rate: per tick</button>
</div>

<script type="importmap">
{ "imports": {
    "three": "__THREE__/build/three.module.js",
    "three/addons/": "__THREE__/examples/jsm/"
} }
</script>

<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const SCENE = __SCENE_JSON__;
const COMMODITY = { item: '#3cb44b', fluid: '#4363d8', power: '#ffd000' };
const FACE_NORMAL = { north: [0,0,-1], south: [0,0,1], east: [1,0,0], west: [-1,0,0],
                      up: [0,1,0], down: [0,-1,0] };

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setSize(window.innerWidth, window.innerHeight);
document.body.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color('#1a1d22');

// Frame on what is actually built, not the solver's oversized search region.
const bmin = new THREE.Vector3(SCENE.bounds.min[0], SCENE.bounds.min[1], SCENE.bounds.min[2]);
const bmax = new THREE.Vector3(SCENE.bounds.max[0], SCENE.bounds.max[1], SCENE.bounds.max[2]);
const center = bmin.clone().add(bmax).multiplyScalar(0.5);
const span = Math.max(bmax.x - bmin.x, bmax.y - bmin.y, bmax.z - bmin.z, 2);

const camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.05, 4000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.enablePan = true;
controls.screenSpacePanning = true;   // pan in the screen plane - the intuitive feel
controls.keyPanSpeed = 16;
controls.listenToKeyEvents(window);    // arrow keys pan too
function resetCamera() {
  camera.position.set(center.x + span * 1.4, center.y + span * 1.3, center.z + span * 1.9);
  controls.target.copy(center);
  controls.update();
}
resetCamera();

scene.add(new THREE.AmbientLight(0xffffff, 0.8));
const sun = new THREE.DirectionalLight(0xffffff, 0.75);
sun.position.set(1, 2, 1.5);
scene.add(sun);

const gspan = Math.ceil(Math.max(bmax.x - bmin.x, bmax.z - bmin.z)) + 2;
const grid = new THREE.GridHelper(gspan, gspan, 0x2c323b, 0x23282f);
grid.position.set(center.x, bmin.y, center.z);
scene.add(grid);
scene.add(new THREE.Box3Helper(new THREE.Box3(bmin.clone(), bmax.clone()), new THREE.Color('#46506a')));

const layered = [];   // { obj, minY, maxY }
function track(obj, minY, maxY) { layered.push({ obj, minY, maxY }); scene.add(obj); }
function cc(c) { return new THREE.Vector3(c[0] + 0.5, c[1] + 0.5, c[2] + 0.5); }

// A square-cross-section bar from a to b (cables, pipes, leads are rectangular, not round).
function bar(a, b, cross, color) {
  const d = new THREE.Vector3().subVectors(b, a);
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(cross, d.length() || 0.001, cross),
    new THREE.MeshStandardMaterial({ color, roughness: 0.5, metalness: 0.1 }));
  mesh.position.copy(a).add(b).multiplyScalar(0.5);
  mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), d.clone().normalize());
  return mesh;
}

function arrow(a, b, color) {
  const g = new THREE.Group();
  const d = new THREE.Vector3().subVectors(b, a);
  const len = d.length() || 0.001;
  const dir = d.clone().normalize();
  const headLen = Math.min(0.32, len * 0.45);
  const shaftLen = Math.max(len - headLen, 0.001);
  const mat = new THREE.MeshStandardMaterial(
    { color, emissive: color, emissiveIntensity: 0.35, roughness: 0.4 });
  const up = new THREE.Vector3(0, 1, 0);
  const shaft = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.07, shaftLen, 12), mat);
  shaft.position.copy(a).addScaledVector(dir, shaftLen * 0.5);
  shaft.quaternion.setFromUnitVectors(up, dir);
  g.add(shaft);
  const head = new THREE.Mesh(new THREE.ConeGeometry(0.17, headLen, 14), mat);
  head.position.copy(a).addScaledVector(dir, shaftLen + headLen * 0.5);
  head.quaternion.setFromUnitVectors(up, dir);
  g.add(head);
  return g;
}

function textColor(hex) {
  const c = parseInt(hex.slice(1), 16);
  const lum = (0.299 * ((c >> 16) & 255) + 0.587 * ((c >> 8) & 255) + 0.114 * (c & 255)) / 255;
  return lum > 0.55 ? '#11141a' : '#f2f4f7';
}

function wrap(ctx, words, maxW) {
  const lines = []; let cur = '';
  for (const w of words) {
    const t = cur ? cur + ' ' + w : w;
    if (ctx.measureText(t).width > maxW && cur) { lines.push(cur); cur = w; } else cur = t;
  }
  if (cur) lines.push(cur);
  return lines;
}

// Name drawn onto the machine's front face (placeholder until real block textures exist).
function frontFace(text, bg, size, normal) {
  const W = 256, H = 256, pad = 20;
  const cnv = document.createElement('canvas');
  cnv.width = W; cnv.height = H;
  const ctx = cnv.getContext('2d');
  ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = textColor(bg);
  let font = 46, lines = [];
  for (; font >= 13; font -= 2) {
    ctx.font = font + 'px monospace';
    lines = wrap(ctx, text.split(' '), W - 2 * pad);
    const fits = lines.every((l) => ctx.measureText(l).width <= W - 2 * pad);
    if (fits && lines.length * font * 1.25 <= H - 2 * pad) break;
  }
  ctx.font = font + 'px monospace';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  const lh = font * 1.25, y0 = H / 2 - (lines.length - 1) * lh / 2;
  lines.forEach((l, i) => ctx.fillText(l, W / 2, y0 + i * lh));

  const axis = Math.abs(normal[0]) > 0 ? 0 : (Math.abs(normal[1]) > 0 ? 1 : 2);
  const dims = axis === 0 ? [size[2], size[1]] : (axis === 1 ? [size[0], size[2]] : [size[0], size[1]]);
  const plane = new THREE.Mesh(
    new THREE.PlaneGeometry(dims[0] * 0.92, dims[1] * 0.92),
    new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(cnv), side: THREE.DoubleSide }));
  return { plane, axis };
}

const centerById = {};
for (const m of SCENE.machines) {
  const [sx, sy, sz] = m.size;
  const pos = new THREE.Vector3(m.cell[0] + sx / 2, m.cell[1] + sy / 2, m.cell[2] + sz / 2);
  centerById[m.id] = pos;
  const minY = m.cell[1], maxY = m.cell[1] + sy - 1;

  const geo = new THREE.BoxGeometry(sx * 0.92, sy * 0.92, sz * 0.92);
  const mat = new THREE.MeshStandardMaterial({ color: m.color, roughness: 0.6, metalness: 0.1 });
  if (m.role === 'source') { mat.emissive = new THREE.Color(m.color); mat.emissiveIntensity = 0.45; }
  const box = new THREE.Mesh(geo, mat);
  box.position.copy(pos);
  track(box, minY, maxY);
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(geo), new THREE.LineBasicMaterial({ color: '#11141a' }));
  edges.position.copy(pos);
  track(edges, minY, maxY);

  const n = FACE_NORMAL[m.front] || FACE_NORMAL.south;
  const { plane, axis } = frontFace(m.type, m.color, m.size, n);
  plane.position.copy(pos).addScaledVector(new THREE.Vector3(n[0], n[1], n[2]), m.size[axis] * 0.46 + 0.012);
  plane.lookAt(pos.x + n[0], pos.y + n[1], pos.z + n[2]);  // face outward, text upright + unmirrored
  track(plane, minY, maxY);
}

for (const r of SCENE.routes) {
  for (const s of r.segments) {
    const cross = r.commodity === 'power' ? 0.09 * Math.sqrt(s.thickness || 1) : 0.07;
    track(bar(cc(s.from), cc(s.to), cross, r.color),
          Math.min(s.from[1], s.to[1]), Math.max(s.from[1], s.to[1]));
  }
  // A short lead from each docked terminal to the machine face, so the cable visibly connects.
  for (const t of (r.terminals || [])) {
    const n = FACE_NORMAL[t.face]; if (!n) continue;
    const term = cc(t.cell);
    const faceMid = term.clone().addScaledVector(new THREE.Vector3(n[0], n[1], n[2]), -0.5);
    const cross = r.commodity === 'power' ? 0.11 : 0.08;
    track(bar(term, faceMid, cross, r.color), t.cell[1], t.cell[1]);
  }
}

for (const ac of SCENE.autoConnections) {
  const a = centerById[ac.source], b = centerById[ac.target];
  if (!a || !b) continue;
  track(arrow(a, b, '#00e5ff'), Math.min(a.y, b.y) | 0, Math.max(a.y, b.y) | 0);
}

const layer = document.getElementById('layer');
const layerVal = document.getElementById('layerVal');
layer.max = String(Math.max(bmax.y - 1, bmin.y));
layer.min = String(bmin.y - 1);
layer.value = String(bmin.y - 1);
function applyLayer() {
  const v = parseInt(layer.value, 10);
  const all = v < bmin.y;
  layerVal.textContent = all ? 'all' : String(v);
  for (const it of layered) it.obj.visible = all || (it.minY <= v && v <= it.maxY);
}
layer.addEventListener('input', applyLayer);
document.getElementById('reset').addEventListener('click', resetCamera);

document.getElementById('hud').firstChild.textContent =
  'status: ' + SCENE.status + '   seed: ' + SCENE.seed +
  '   build ' + (bmax.x - bmin.x) + 'x' + (bmax.y - bmin.y) + 'x' + (bmax.z - bmin.z) +
  '   machines ' + SCENE.machines.length;

// System-I/O rates are stored per tick; the toggle re-renders them as per second (x20). Cable
// amperage (byTier) is a steady value, so it never scales with the time unit.
let perSecond = false;
const TICKS_PER_SECOND = 20;
function fmtRate(perTick) {
  const v = perSecond ? perTick * TICKS_PER_SECOND : perTick;
  return Math.round(v * 1e4) / 1e4;  // trim binary-float noise (e.g. 0.1 * 20)
}
function renderLegend() {
  let html = '<b>machines</b><br>';
  for (const e of SCENE.legend) html += '<span class="sw" style="background:' + e.color + '"></span>' + e.label + '<br>';
  html += '<b>routes</b><br>';
  for (const k of ['item', 'fluid', 'power']) html += '<span class="sw" style="background:' + COMMODITY[k] + '"></span>' + k + '<br>';
  html += '<span class="sw" style="background:#00e5ff"></span>auto-output<br>';
  if (SCENE.io) {
    const io = SCENE.io, sfx = perSecond ? '/s' : '/t';
    html += '<b>system i/o</b><br>';
    for (const i of io.inputs)
      html += 'in: ' + i.resource + (i.rate != null ? ' (~' + fmtRate(i.rate) + ' ' + i.unit + sfx + ')' : '') + '<br>';
    for (const o of io.outputs)
      html += 'out: ' + o.resource + (o.rate != null ? ' (~' + fmtRate(o.rate) + ' ' + o.unit + sfx + ')' : '') + '<br>';
    const tiers = Object.keys(io.power.byTier);
    const byTier = tiers.map((t) => t + ' ' + io.power.byTier[t] + 'A').join(', ');
    html += 'power: ' + fmtRate(io.power.total) + ' EU' + sfx + (tiers.length ? ' (' + byTier + ')' : '') + '<br>';
  }
  document.getElementById('legend').innerHTML = html;
}
renderLegend();
document.getElementById('rateUnit').addEventListener('click', () => {
  perSecond = !perSecond;
  document.getElementById('rateUnit').textContent = 'rate: per ' + (perSecond ? 'second' : 'tick');
  renderLegend();
});

window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

function animate() { requestAnimationFrame(animate); controls.update(); renderer.render(scene, camera); }
applyLayer();
animate();
</script>
</body>
</html>
"""
).replace("__THREE__", _THREE)


def render_html(scene: dict[str, Any]) -> str:
    """Return a self-contained viewer page with ``scene`` (from ``build_scene``) inlined."""
    return _TEMPLATE.replace(_SCENE_TOKEN, json.dumps(scene))
