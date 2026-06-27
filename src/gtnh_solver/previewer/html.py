"""previewer.html - wrap the scene dict in a single, self-contained three.js viewer page.

``render_html`` injects ``build_scene``'s dict (as JSON) into a static template: one double-
clickable ``.html`` that pulls three.js from a CDN and draws the layout - orbit/pan/zoom camera
and a layer-by-layer slider (the two things a builder needs). The scene JSON is *inlined*, not
fetched, so there is no ``file://`` CORS problem. The template is assembled by replacing a single
``__SCENE_JSON__`` token (NOT an f-string / ``.format``) so the JS/CSS braces stay literal.

Vendoring three.js to drop the CDN (offline viewing) is a noted follow-up; v1 uses the CDN per
the architecture's previewer note.
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
  #legend { top: 10px; right: 10px; max-height: 80vh; overflow: auto; }
  #controls { bottom: 10px; left: 10px; display: flex; gap: 12px; align-items: center; }
  #controls input[type=range] { width: 160px; }
  .sw { display: inline-block; width: 11px; height: 11px; margin-right: 6px; border-radius: 2px;
        vertical-align: middle; }
  b { color: #aab2bd; font-weight: 600; }
  button { font: inherit; color: #e8eaed; background: #2a2f37; border: 1px solid #3a4150;
           border-radius: 4px; padding: 3px 8px; cursor: pointer; }
</style>
</head>
<body>
<div id="hud">loading...</div>
<div id="legend"></div>
<div id="controls">
  <span>layer <b id="layerVal">all</b></span>
  <input id="layer" type="range" min="-1" max="0" value="-1" step="1">
  <label><input id="labels" type="checkbox" checked> labels</label>
  <button id="reset">reset camera</button>
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
const region = SCENE.region;
const COMMODITY = { item: '#3cb44b', fluid: '#4363d8', power: '#ffd000' };

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setSize(window.innerWidth, window.innerHeight);
document.body.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color('#1a1d22');

const camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.1, 2000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

const center = new THREE.Vector3(region.sx / 2, region.sy / 2, region.sz / 2);
const span = Math.max(region.sx, region.sy, region.sz, 4);
function resetCamera() {
  camera.position.set(center.x + span * 1.2, center.y + span * 1.1, center.z + span * 1.4);
  controls.target.copy(center);
  controls.update();
}
resetCamera();

scene.add(new THREE.AmbientLight(0xffffff, 0.75));
const sun = new THREE.DirectionalLight(0xffffff, 0.8);
sun.position.set(1, 2, 1.5);
scene.add(sun);

const grid = new THREE.GridHelper(span * 2, span * 2, 0x2c323b, 0x23282f);
grid.position.set(center.x, 0, center.z);
scene.add(grid);
const regionBox = new THREE.Box3(
  new THREE.Vector3(0, 0, 0),
  new THREE.Vector3(region.sx, region.sy, region.sz));
scene.add(new THREE.Box3Helper(regionBox, new THREE.Color('#3a4150')));

const layered = [];   // { obj, minY, maxY, isLabel? }
let labelsOn = true;

function cc(c) { return new THREE.Vector3(c[0] + 0.5, c[1] + 0.5, c[2] + 0.5); }

function makeLabel(text) {
  const cnv = document.createElement('canvas');
  const ctx = cnv.getContext('2d');
  ctx.font = '24px monospace';
  cnv.width = Math.ceil(ctx.measureText(text).width) + 16;
  cnv.height = 34;
  ctx.font = '24px monospace';
  ctx.fillStyle = 'rgba(18,20,26,0.85)';
  ctx.fillRect(0, 0, cnv.width, cnv.height);
  ctx.fillStyle = '#e8eaed';
  ctx.fillText(text, 8, 25);
  const spr = new THREE.Sprite(new THREE.SpriteMaterial(
    { map: new THREE.CanvasTexture(cnv), depthTest: false }));
  spr.scale.set((cnv.width / cnv.height) * 0.55, 0.55, 1);
  return spr;
}

function cylinder(a, b, radius, color) {
  const d = new THREE.Vector3().subVectors(b, a);
  const mesh = new THREE.Mesh(
    new THREE.CylinderGeometry(radius, radius, d.length() || 0.001, 10),
    new THREE.MeshStandardMaterial({ color, roughness: 0.5 }));
  mesh.position.copy(a).add(b).multiplyScalar(0.5);
  mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), d.clone().normalize());
  return mesh;
}

const centerById = {};
for (const m of SCENE.machines) {
  const [sx, sy, sz] = m.size;
  const pos = new THREE.Vector3(m.cell[0] + sx / 2, m.cell[1] + sy / 2, m.cell[2] + sz / 2);
  centerById[m.id] = pos;
  const geo = new THREE.BoxGeometry(sx * 0.92, sy * 0.92, sz * 0.92);
  const mat = new THREE.MeshStandardMaterial({ color: m.color, roughness: 0.6, metalness: 0.1 });
  if (m.role === 'source') { mat.emissive = new THREE.Color(m.color); mat.emissiveIntensity = 0.45; }
  const box = new THREE.Mesh(geo, mat);
  box.position.copy(pos);
  scene.add(box);
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(geo), new THREE.LineBasicMaterial({ color: '#11141a' }));
  edges.position.copy(pos);
  scene.add(edges);
  const label = makeLabel(m.type);
  label.position.set(pos.x, pos.y + sy / 2 + 0.5, pos.z);
  scene.add(label);
  const minY = m.cell[1], maxY = m.cell[1] + sy - 1;
  layered.push({ obj: box, minY, maxY });
  layered.push({ obj: edges, minY, maxY });
  layered.push({ obj: label, minY, maxY, isLabel: true });
}

for (const r of SCENE.routes) {
  for (const s of r.segments) {
    const radius = r.commodity === 'power' ? 0.05 * Math.sqrt(s.thickness || 1) : 0.04;
    const tube = cylinder(cc(s.from), cc(s.to), radius, r.color);
    scene.add(tube);
    layered.push({ obj: tube,
      minY: Math.min(s.from[1], s.to[1]), maxY: Math.max(s.from[1], s.to[1]) });
  }
}

for (const ac of SCENE.autoConnections) {
  const a = centerById[ac.source], b = centerById[ac.target];
  if (!a || !b) continue;
  const d = new THREE.Vector3().subVectors(b, a);
  const arrow = new THREE.ArrowHelper(d.clone().normalize(), a.clone(), d.length(), 0xffffff, 0.32, 0.18);
  scene.add(arrow);
  layered.push({ obj: arrow, minY: Math.min(a.y, b.y) | 0, maxY: Math.max(a.y, b.y) | 0 });
}

const layer = document.getElementById('layer');
const layerVal = document.getElementById('layerVal');
layer.max = String(Math.max(region.sy - 1, 0));
function applyLayer() {
  const v = parseInt(layer.value, 10);
  const all = v < 0;
  layerVal.textContent = all ? 'all' : String(v);
  for (const it of layered) {
    it.obj.visible = (all || (it.minY <= v && v <= it.maxY)) && (!it.isLabel || labelsOn);
  }
}
layer.addEventListener('input', applyLayer);
document.getElementById('labels').addEventListener('change', (e) => {
  labelsOn = e.target.checked; applyLayer();
});
document.getElementById('reset').addEventListener('click', resetCamera);

document.getElementById('hud').textContent =
  'status: ' + SCENE.status + '   seed: ' + SCENE.seed +
  '   region ' + region.sx + 'x' + region.sy + 'x' + region.sz +
  '   machines ' + SCENE.machines.length;

let html = '<b>machines</b><br>';
for (const e of SCENE.legend) html += '<span class="sw" style="background:' + e.color + '"></span>' + e.label + '<br>';
html += '<b>routes</b><br>';
for (const k of ['item', 'fluid', 'power']) html += '<span class="sw" style="background:' + COMMODITY[k] + '"></span>' + k + '<br>';
document.getElementById('legend').innerHTML = html;

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
