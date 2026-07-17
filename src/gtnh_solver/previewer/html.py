"""previewer.html - wrap the scene dict in a single, self-contained three.js viewer page.

``render_html`` injects ``build_scene``'s dict (as JSON) into a static template: one double-
clickable ``.html`` that pulls three.js from a CDN and draws the layout. The camera orbits AND
pans (right-drag / arrow keys), and a layer-by-layer slider isolates each y-level. Machines are
solid boxes skinned with their real GT casing texture where ``scene.textures`` supplies one (the
six per-face icons ride ``machine.texture``; missing icons fall back to the flat type colour), with
the machine name on the front face; a state control swaps every machine between its idle and running
skin where the two differ (the running faces ride ``scene.texturesActive``, default idle); routes
(cables and
pipes) are drawn GT-style, a small cube at each cell centre with a uniform arm out to the block edge
for every connection (an adjacent route cell or a docked machine face), power sized by cable
thickness - each wire->machine lead by the terminal's incident-segment thickness the scene emits
(#6); auto-output is a small arrow on each source-machine face perpendicular to the ejecting
direction (so one stays visible however the machines are packed). A side panel lists the
machine/route legend plus the
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
  <button id="stateToggle" title="toggle machine idle / running skins">state: idle</button>
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
const COMMODITY = Object.fromEntries(SCENE.routeLegend.map((e) => [e.commodity, e.color]));
// ^ route colours read from the scene (SCENE.routeLegend), one source - not a 2nd hard-coded copy.
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

// Align grid lines to integer cell boundaries so they frame the blocks instead of cutting through
// them (#19). A GridHelper's lines sit at integer offsets from its center only when the division
// count is even; pairing that with an integer-snapped center lands every line on a cell edge.
const gspanRaw = Math.ceil(Math.max(bmax.x - bmin.x, bmax.z - bmin.z)) + 2;
const gspan = gspanRaw + (gspanRaw % 2);
const grid = new THREE.GridHelper(gspan, gspan, 0x2c323b, 0x23282f);
grid.position.set(Math.round(center.x), bmin.y, Math.round(center.z));
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

// A small cube at a cell centre - the node a route's connection arms fan out from.
function node(pos, size, color) {
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(size, size, size),
    new THREE.MeshStandardMaterial({ color, roughness: 0.5, metalness: 0.1 }));
  mesh.position.copy(pos);
  return mesh;
}

// A small flat arrow decal (a canvas texture on a plane) pointing along the plane's local +x.
function faceArrow(color) {
  const S = 128, cnv = document.createElement('canvas');
  cnv.width = S; cnv.height = S;
  const ctx = cnv.getContext('2d');
  ctx.fillStyle = color;
  ctx.beginPath();                 // an arrowhead + shaft pointing +x (right)
  ctx.moveTo(0.14 * S, 0.40 * S);
  ctx.lineTo(0.55 * S, 0.40 * S);
  ctx.lineTo(0.55 * S, 0.24 * S);
  ctx.lineTo(0.90 * S, 0.50 * S);
  ctx.lineTo(0.55 * S, 0.76 * S);
  ctx.lineTo(0.55 * S, 0.60 * S);
  ctx.lineTo(0.14 * S, 0.60 * S);
  ctx.closePath();
  ctx.fill();
  // alphaTest (not transparent) so it renders in the opaque pass with normal depth testing. The arrow
  // is positioned just OUTSIDE the machine's rendered surface (see the autoConnections loop), so it
  // draws on top of the casing texture and the opaque name plate rather than being buried under either,
  // while still being occluded by any other machine that sits in front of it (GitHub #30).
  return new THREE.Mesh(
    new THREE.PlaneGeometry(0.25, 0.25),
    new THREE.MeshBasicMaterial(
      { map: new THREE.CanvasTexture(cnv), alphaTest: 0.5, side: THREE.DoubleSide }));
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

// Name drawn onto the machine's front face - kept even when the box is textured, so the five other
// faces show the GT casing texture while the front stays the readable identity label. The fill is
// opaque so the text keeps a flat, high-contrast backing; the auto-output arrow is lifted clear of
// this plane so it still draws on top (GitHub #30 - see the autoConnections loop).
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

// Real GT block textures are pre-baked per (block, meta, side, state) into flat PNGs and embedded in
// SCENE.textures (pool key -> data: URI). A machine that resolved its structure is drawn as ONE
// nearest-filtered cube PER constituent block (SCENE.blocks), not a single stretched box - so coils,
// glass, hatch faces, and the internal structure stay visible (principle 6). A machine with no
// committed doc, or whose blocks did not bake, keeps its flat colour placeholder box.
// Each machine face is baked idle (SCENE.textures) and, where the running skin differs, ALSO active
// (SCENE.texturesActive - only the faces with an _ACTIVE overlay, so the page never carries a second
// copy of an identical texture). The default render is idle; the #stateToggle control swaps the
// registered face materials to their active map (see stateMaterials).
const TEXTURES = SCENE.textures || {};
const TEXTURES_ACTIVE = SCENE.texturesActive || {};
const _texCache = {}, _texCacheActive = {};
const stateMaterials = [];   // { mat, idle, active } for faces whose running skin actually differs
function loadTex(uri) {
  const tex = new THREE.TextureLoader().load(uri);
  tex.magFilter = THREE.NearestFilter;    // crisp pixel art, no bilinear smear
  tex.minFilter = THREE.NearestFilter;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}
function faceTexture(key) {
  if (!key) return null;
  if (key in _texCache) return _texCache[key];
  const uri = TEXTURES[key];
  _texCache[key] = uri ? loadTex(uri) : null;
  return _texCache[key];
}
function faceTextureActive(key) {
  if (!key || !(key in TEXTURES_ACTIVE)) return null;   // no distinct running skin for this face
  if (key in _texCacheActive) return _texCacheActive[key];
  _texCacheActive[key] = loadTex(TEXTURES_ACTIVE[key]);
  return _texCacheActive[key];
}
function flatMaterial(m) {
  const mm = new THREE.MeshStandardMaterial({ color: m.color, roughness: 0.6, metalness: 0.1 });
  if (m.role === 'source') { mm.emissive = new THREE.Color(m.color); mm.emissiveIntensity = 0.45; }
  return mm;
}
// A per-block cube's six materials from its baked-face pool keys (three.js material order). A face
// with no baked texture falls back to a neutral casing grey so the cube still reads as a block.
const _UNBAKED = new THREE.MeshStandardMaterial({ color: '#6b7280', roughness: 0.8, metalness: 0.05 });
function blockMaterials(faces) {
  return faces.map((key) => {
    const tex = faceTexture(key);
    if (!tex) return _UNBAKED;
    const mat = new THREE.MeshStandardMaterial({ map: tex, roughness: 0.8, metalness: 0.03 });
    const active = faceTextureActive(key);
    if (active) stateMaterials.push({ mat, idle: tex, active });   // swappable by #stateToggle
    return mat;
  });
}

const centerById = {}, sizeById = {}, expandedById = {};
for (const m of SCENE.machines) {
  const [sx, sy, sz] = m.size;
  const pos = new THREE.Vector3(m.cell[0] + sx / 2, m.cell[1] + sy / 2, m.cell[2] + sz / 2);
  centerById[m.id] = pos;
  sizeById[m.id] = m.size;
  expandedById[m.id] = !!m.expanded;   // full-size textured cubes below vs the 0.92-scaled placeholder box
  const minY = m.cell[1], maxY = m.cell[1] + sy - 1;

  // Expanded machines are drawn below as per-block cubes; skip the box + name-plate for them so a
  // textured multiblock shows its real structure instead of a smeared placeholder shell.
  if (m.expanded) continue;

  const geo = new THREE.BoxGeometry(sx * 0.92, sy * 0.92, sz * 0.92);
  const box = new THREE.Mesh(geo, flatMaterial(m));
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

// Per-block cubes: one nearest-filtered 1x1x1 cube per constituent block of every expanded machine
// (SCENE.blocks), each of its six faces textured from the baked pool key the scene resolved. This is
// the principle-6 render - a multiblock shows its casings, coils, glass, and hatch faces as distinct
// blocks instead of one stretched box. Blocks sit flush (real GT blocks touch); the per-block texture
// pattern is what makes the internal structure legible.
for (const b of (SCENE.blocks || [])) {
  const geo = new THREE.BoxGeometry(1, 1, 1);
  const cube = new THREE.Mesh(geo, blockMaterials(b.texture));
  cube.position.set(b.cell[0] + 0.5, b.cell[1] + 0.5, b.cell[2] + 0.5);
  track(cube, b.cell[1], b.cell[1]);
}

// A route is drawn GT-style: a small cube at each cell centre, with a UNIFORM cross-section arm from
// that cube out to the block edge for every connection - an adjacent route cell, or a docked machine
// face. One node per cell keeps the run readable however tightly the routes are packed.
for (const r of SCENE.routes) {
  const isPower = r.commodity === 'power';
  const cells = new Map();   // "x,y,z" -> { cell, dirs: Set of "dx,dy,dz", thick }
  const touch = (c, thick) => {
    const k = c.join(',');
    let e = cells.get(k);
    if (!e) { e = { cell: c, dirs: new Set(), thick: 1 }; cells.set(k, e); }
    e.thick = Math.max(e.thick, thick);
    return e;
  };
  for (const s of r.segments) {
    const a = s.from, b = s.to, th = s.thickness || 1;
    touch(a, th).dirs.add([b[0] - a[0], b[1] - a[1], b[2] - a[2]].join(','));
    touch(b, th).dirs.add([a[0] - b[0], a[1] - b[1], a[2] - b[2]].join(','));
  }
  for (const t of (r.terminals || [])) {
    const nrm = FACE_NORMAL[t.face]; if (!nrm) continue;
    // an arm toward the machine - the lead - sized by the incident segment's thickness, computed
    // in build_scene (#6). Null (item/fluid) falls back to 1, keeping those leads the fixed size.
    touch(t.cell, t.thickness || 1).dirs.add([-nrm[0], -nrm[1], -nrm[2]].join(','));
  }
  for (const e of cells.values()) {
    const cross = isPower ? 0.09 * Math.sqrt(e.thick) : 0.07;
    const c = cc(e.cell);
    track(node(c, cross, r.color), e.cell[1], e.cell[1]);
    for (const dk of e.dirs) {
      const d = dk.split(',').map(Number);
      const end = c.clone().add(new THREE.Vector3(d[0] * 0.5, d[1] * 0.5, d[2] * 0.5));
      track(bar(c, end, cross, r.color), e.cell[1], e.cell[1]);
    }
  }
}

// Auto-output: a small arrow on each source-machine face whose plane CONTAINS the ejecting
// direction - the two side faces perpendicular to it plus the top and bottom. (The output face and
// its opposite can't show an in-plane arrow.) At least one is visible from any angle, however
// tightly the machines are packed together, so the flow direction is never fully occluded.
for (const ac of SCENE.autoConnections) {
  const src = centerById[ac.source], n = FACE_NORMAL[ac.sourceFace];
  if (!src || !n) continue;
  const size = sizeById[ac.source] || [1, 1, 1], cellY = Math.round(src.y - size[1] / 2);
  // Sit the arrow just OUTSIDE the machine's rendered surface so it is never buried in the geometry:
  // an expanded machine draws full-size (0.50 half-extent) textured block cubes, a placeholder its
  // 0.92-scaled box (0.46). The extra 0.03 also clears the front name plate (+0.012), so the arrow
  // draws on top of the texture AND the label; normal depth testing still hides it behind any machine
  // that is actually in front of it.
  const surf = expandedById[ac.source] ? 0.50 : 0.46, lift = 0.03;
  const nv = new THREE.Vector3(n[0], n[1], n[2]);
  for (const m of [[1,0,0],[-1,0,0],[0,1,0],[0,-1,0],[0,0,1],[0,0,-1]]) {
    if (m[0]*n[0] + m[1]*n[1] + m[2]*n[2] !== 0) continue;   // skip the output face and its opposite
    const mv = new THREE.Vector3(m[0], m[1], m[2]);
    const alongM = Math.abs(m[0])*size[0] + Math.abs(m[1])*size[1] + Math.abs(m[2])*size[2];
    const alongN = Math.abs(n[0])*size[0] + Math.abs(n[1])*size[1] + Math.abs(n[2])*size[2];
    const deco = faceArrow('#00e5ff');
    deco.position.copy(src)
      .addScaledVector(mv, surf * alongM + lift)    // just outside the rendered face + name plate -> on top
      .addScaledVector(nv, surf * alongN - 0.10);   // slide toward the output edge so the tip reaches it
    deco.quaternion.setFromRotationMatrix(
      new THREE.Matrix4().makeBasis(nv, new THREE.Vector3().crossVectors(mv, nv), mv));
    track(deco, cellY, cellY + size[1] - 1);
  }
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

// Idle <-> running skin toggle. Only faces with a distinct active bake are registered, so the swap
// touches those materials alone; a layout with none (every machine identical at rest and running)
// disables the control rather than showing a dead no-op button.
const stateToggle = document.getElementById('stateToggle');
if (stateMaterials.length === 0) {
  stateToggle.disabled = true;
  stateToggle.title = 'no running-state textures in this layout';
} else {
  let running = false;
  stateToggle.addEventListener('click', () => {
    running = !running;
    stateToggle.textContent = 'state: ' + (running ? 'running' : 'idle');
    for (const s of stateMaterials) { s.mat.map = running ? s.active : s.idle; s.mat.needsUpdate = true; }
  });
}

document.getElementById('hud').firstChild.textContent =
  'status: ' + SCENE.status + '   seed: ' + SCENE.seed +
  '   build ' + (bmax.x - bmin.x) + 'x' + (bmax.y - bmin.y) + 'x' + (bmax.z - bmin.z) +
  '   machines ' + SCENE.machines.length;

// System-I/O rates are stored per tick; the toggle re-renders them as per second (x20). Cable
// amperage (byTier) is a steady value, so it never scales with the time unit.
let perSecond = false;
const TICKS_PER_SECOND = 20;
// Render a rate at 6 significant figures. The plan value is exact, so prefix '~' ONLY when that
// 6-sig-fig form actually loses precision (a non-terminating decimal like 25/12); exact rates
// (0.1, 6.25, ...) print clean. The relative tolerance ignores binary-float noise (e.g. 0.1 * 20).
function rateText(perTick) {
  const v = perSecond ? perTick * TICKS_PER_SECOND : perTick;
  const shown = parseFloat(v.toPrecision(6));
  const rounded = Math.abs(shown - v) > Math.abs(v) * 1e-9;
  return (rounded ? '~' : '') + shown;
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
      html += 'in: ' + i.resource + (i.rate != null ? ' (' + rateText(i.rate) + ' ' + i.unit + sfx + ')' : '') + '<br>';
    for (const o of io.outputs)
      html += 'out: ' + o.resource + (o.rate != null ? ' (' + rateText(o.rate) + ' ' + o.unit + sfx + ')' : '') + '<br>';
    // Power: total EU/t supplied plus the per-tier feed spec, the full tier voltage x amps to
    // supply (how a GT source is fed). The total is that feed (tier voltage x amps), so it matches
    // the breakdown, e.g. 'power: 96 EU/t (LV 32V x 3A)' where 96 = 32 x 3.
    const tiers = Object.keys(io.power.byTier);
    const feed = tiers.map((t) => t + ' ' + io.power.byTier[t].volts + 'V x ' + io.power.byTier[t].amps + 'A').join(', ');
    html += 'power: ' + rateText(io.power.total) + ' EU' + sfx + (tiers.length ? ' (' + feed + ')' : '') + '<br>';
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
    # Plan JSON is external input: escape ``</`` so a machine type or resource id containing
    # ``</script>`` cannot close this inline <script> and break (or inject into) the page. The
    # JS parser reads ``<\/script>`` back as the identical string.
    payload = json.dumps(scene).replace("</", "<\\/")
    return _TEMPLATE.replace(_SCENE_TOKEN, payload)
