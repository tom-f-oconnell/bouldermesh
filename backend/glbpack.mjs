// Packs a reconstruction's GLB for the viewer to download (glbpack.py says what for, and runs this with node or deno):
//
//     glbpack.mjs <source.glb> <out dir> <preview faces> <make a preview from faces>
//
// writes into <out dir>
//   full.glb             the same meshes, primitives and faces, compressed with meshoptimizer (EXT_meshopt_compression):
//                        positions as floats with fewer bits (its exponential filter), so in the same coordinates,
//                        texture coordinates as 16-bit integers, faces in the order that compresses best (and each
//                        may start at another of its corners, which keeps the way it faces)
//   full.faces.bin       for each of its faces, in order, the source's face it is (a uint32 index over all primitives)
//   preview.glb          if <preview faces> isn't 0 and the source has at least <make a preview from> faces: simplified to
//                        about <preview faces>,
//   preview.faces.bin    and for each of its faces a source face it shares a corner with, so in the same piece
// and prints what it made as JSON. It decodes what it wrote and checks it against the source before it says so.
import fs from 'node:fs';
import { MeshoptEncoder as E } from './vendor/meshoptimizer/meshopt_encoder.mjs';
import { MeshoptSimplifier as S } from './vendor/meshoptimizer/meshopt_simplifier.mjs';
import { MeshoptDecoder as D } from './vendor/meshoptimizer/meshopt_decoder.mjs';

const POSITION_BITS = 16, NORMAL_BITS = 12;
const PREVIEW_ERROR = 0.01;      // the most a preview may differ from the source, as a share of its size
const GLB = 0x46546c67, JSON_CHUNK = 0x4e4f534a, BIN_CHUNK = 0x004e4942;
const TYPES = { 5126: Float32Array, 5125: Uint32Array, 5123: Uint16Array, 5121: Uint8Array };
const WIDTH = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 };
const UNSUPPORTED = ['KHR_draco_mesh_compression', 'EXT_meshopt_compression', 'KHR_meshopt_compression', 'KHR_mesh_quantization',
  'EXT_mesh_gpu_instancing'];

function readGlb(buf) {
  if (buf.readUInt32LE(0) !== GLB || buf.readUInt32LE(16) !== JSON_CHUNK) throw new Error('not a GLB file');
  const n = buf.readUInt32LE(12), json = JSON.parse(buf.toString('utf8', 20, 20 + n)), at = 20 + n;
  const bin = at + 8 <= buf.length && buf.readUInt32LE(at + 4) === BIN_CHUNK ? buf.subarray(at + 8, at + 8 + buf.readUInt32LE(at)) : null;
  return { json, bin };
}

// A buffer: the BIN chunk, or one in a data URI, as OpenMVS writes the second when it splits a texture in two
function buffer(src, b) {
  src.buffers ??= {};
  if (src.buffers[b]) return src.buffers[b];
  const uri = src.json.buffers[b].uri;
  if (uri === undefined && b === 0 && src.bin) return src.buffers[b] = src.bin;
  const data = /^data:[^,]*;base64,(.*)$/s.exec(uri || '');
  if (!data) throw new Error('only geometry inside the GLB is supported');
  return src.buffers[b] = Buffer.from(data[1], 'base64');
}

function accessor(src, i) {
  const a = src.json.accessors[i], T = TYPES[a.componentType], width = WIDTH[a.type];
  if (!T || !width || a.sparse || a.bufferView === undefined) throw new Error('unusual accessors aren\'t supported');
  const v = src.json.bufferViews[a.bufferView], bytes = buffer(src, v.buffer || 0);
  if (v.byteStride && v.byteStride !== T.BYTES_PER_ELEMENT * width) throw new Error('interleaved geometry isn\'t supported');
  const start = bytes.byteOffset + (v.byteOffset || 0) + (a.byteOffset || 0), length = a.count * width;
  return { values: new T(bytes.buffer.slice(start, start + length * T.BYTES_PER_ELEMENT)), width, count: a.count, a };
}

// For each face of `after`, which of `before`'s it is: the same corners, in the same order, after `remap`. Reordering for
// the vertex cache moves whole faces without turning them.
function facesMoved(before, remap, after) {
  const n = after.length / 3, head = new Int32Array(remap.length).fill(-1), next = new Int32Array(n), from = new Uint32Array(n);
  const taken = new Uint8Array(n);
  for (let j = n - 1; j >= 0; j--) { next[j] = head[after[3 * j]]; head[after[3 * j]] = j; }
  for (let i = 0; i < n; i++) {
    const a = remap[before[3 * i]], b = remap[before[3 * i + 1]], c = remap[before[3 * i + 2]];
    let j = head[a];
    while (j >= 0 && (taken[j] || after[3 * j + 1] !== b || after[3 * j + 2] !== c)) j = next[j];
    if (j < 0) throw new Error(`face ${i} was lost in reordering`);
    taken[j] = 1; from[j] = i;
  }
  return from;
}

// A primitive's faces (in the source's vertex numbering, with `origin`: the source face each comes from), reordered to
// compress well, with only the vertices they use. Returns the new faces, the vertex each new one was, and their origins.
function reorder(faces, origin) {
  const order = faces.slice();
  const [remap, unique] = E.reorderMesh(order, true, false);
  const moved = facesMoved(faces, remap, order), origins = new Uint32Array(moved.length);
  for (let j = 0; j < moved.length; j++) origins[j] = origin[moved[j]];
  const was = new Uint32Array(unique);
  for (let v = 0; v < remap.length; v++) if (remap[v] !== 0xffffffff) was[remap[v]] = v;
  return { faces: order, was, origins };
}

function gather(values, width, was) {
  const out = new values.constructor(was.length * width);
  for (let i = 0; i < was.length; i++) for (let k = 0; k < width; k++) out[i * width + k] = values[was[i] * width + k];
  return out;
}

const decodeExp = (bytes, n) => {    // the floats the exponential filter's output stands for, as a decoder makes them
  const ints = new Int32Array(bytes.buffer, bytes.byteOffset, n), out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = (ints[i] << 8 >> 8) * 2 ** (ints[i] >> 24);
  return out;
};

class Writer {
  constructor() { this.parts = []; this.packed = 0; this.plain = 0; this.views = []; this.accessors = []; }
  view(raw, count, stride, mode, filter, target) {
    const data = E.encodeGltfBuffer(raw, count, stride, mode);
    const pad = -this.packed % 4 & 3;
    if (pad) this.parts.push(new Uint8Array(pad));
    this.packed += pad;
    this.plain += -this.plain % 4 & 3;
    const ext = { buffer: 0, byteOffset: this.packed, byteLength: data.length, byteStride: stride, count, mode };
    if (filter) ext.filter = filter;
    this.views.push({ buffer: 1, byteOffset: this.plain, byteLength: count * stride, ...(target === 34962 ? { byteStride: stride } : {}), target,
      extensions: { EXT_meshopt_compression: ext } });
    this.parts.push(data);
    this.packed += data.length; this.plain += count * stride;
    return this.views.length - 1;
  }
  accessor(view, componentType, count, type, extra = {}) {
    this.accessors.push({ bufferView: view, componentType, count, type, ...extra });
    return this.accessors.length - 1;
  }
}

function attribute(w, name, values, width, count) {
  if (!(values instanceof Float32Array)) throw new Error(`${name} isn't floats, which isn't supported`);
  if (name === 'POSITION' || name === 'NORMAL') {
    if (width !== 3) throw new Error(`${name} isn't 3D`);
    const bits = name === 'POSITION' ? POSITION_BITS : NORMAL_BITS;
    const raw = E.encodeFilterExp(values, count, 12, bits, 'SharedComponent'), shown = decodeExp(raw, count * 3);
    const extra = {};
    if (name === 'POSITION') {
      extra.min = [Infinity, Infinity, Infinity]; extra.max = [-Infinity, -Infinity, -Infinity];
      for (let i = 0; i < shown.length; i++) { const k = i % 3; if (shown[i] < extra.min[k]) extra.min[k] = shown[i]; if (shown[i] > extra.max[k]) extra.max[k] = shown[i]; }
    }
    return { index: w.accessor(w.view(raw, count, 12, 'ATTRIBUTES', 'EXPONENTIAL', 34962), 5126, count, 'VEC3', extra), shown, kind: 'float' };
  }
  if (name.startsWith('TEXCOORD_') && width === 2 && values.every(x => x >= 0 && x <= 1)) {
    const q = new Uint16Array(values.length);
    for (let i = 0; i < values.length; i++) q[i] = Math.round(values[i] * 65535);
    return { index: w.accessor(w.view(new Uint8Array(q.buffer), count, 4, 'ATTRIBUTES', null, 34962), 5123, count, 'VEC2', { normalized: true }), shown: q, kind: 'unorm16' };
  }
  if (name === 'COLOR_0' && (width === 3 || width === 4)) {     // as RGBA, as a vertex attribute's stride must be a multiple of 4
    const q = new Uint16Array(count * 4);
    for (let i = 0; i < count; i++) for (let k = 0; k < 4; k++) q[i * 4 + k] = k < width ? Math.round(Math.min(1, Math.max(0, values[i * width + k])) * 65535) : 65535;
    return { index: w.accessor(w.view(new Uint8Array(q.buffer), count, 8, 'ATTRIBUTES', null, 34962), 5123, count, 'VEC4', { normalized: true }), shown: q, kind: 'unorm16' };
  }
  throw new Error(`the attribute ${name} isn't supported`);
}

// One version of the source: its faces given by make(primitive), which returns {faces, origin} in the source's numbering
function pack(src, make, lod) {
  const { json } = src, w = new Writer(), out = structuredClone(json), checks = [];
  let total = 0;
  out.meshes = json.meshes.map((m, mi) => ({ ...m, primitives: m.primitives.map((p, pi) => {
    const prim = src.prims[`${mi}/${pi}`], made = make(prim);
    const { faces, was, origins } = reorder(made.faces, made.origin);
    const attributes = {}, shown = {};
    for (const [name, a] of Object.entries(prim.attributes)) {
      const r = attribute(w, name, gather(a.values, a.width, was), a.width, was.length);
      attributes[name] = r.index; shown[name] = r;
    }
    const indices = w.accessor(w.view(new Uint8Array(faces.buffer), faces.length, 4, 'TRIANGLES', null, 34963), 5125, faces.length, 'SCALAR');
    checks.push({ prim, faces, was, origins, shown, offset: prim.offset });
    total += faces.length / 3;
    return { ...p, attributes, indices };
  }) }));
  const packed = new Uint8Array(w.packed + (-w.packed % 4 & 3));
  let at = 0;
  for (const part of w.parts) { packed.set(part, at); at += part.length; }
  out.accessors = w.accessors; out.bufferViews = w.views;
  out.buffers = [{ byteLength: packed.length }, { byteLength: w.plain, extensions: { EXT_meshopt_compression: { fallback: true } } }];
  out.extensionsUsed = [...new Set([...(json.extensionsUsed || []), 'EXT_meshopt_compression'])];
  out.extensionsRequired = [...new Set([...(json.extensionsRequired || []), 'EXT_meshopt_compression'])];
  out.asset = { ...json.asset, extras: { ...(json.asset?.extras || {}), bouldermesh: { lod, faces: total } } };
  let text = Buffer.from(JSON.stringify(out));
  text = Buffer.concat([text, Buffer.alloc(-text.length % 4 & 3, 0x20)]);
  const head = Buffer.alloc(12);
  head.writeUInt32LE(GLB, 0); head.writeUInt32LE(2, 4); head.writeUInt32LE(12 + 8 + text.length + 8 + packed.length, 8);
  const chunk = (n, kind) => { const b = Buffer.alloc(8); b.writeUInt32LE(n, 0); b.writeUInt32LE(kind, 4); return b; };
  const glb = Buffer.concat([head, chunk(text.length, JSON_CHUNK), text, chunk(packed.length, BIN_CHUNK), Buffer.from(packed.buffer)]);
  const origins = new Uint32Array(total);
  let k = 0;
  for (const c of checks) for (let j = 0; j < c.origins.length; j++) origins[k++] = c.offset + c.origins[j];
  return { glb, origins, checks, faces: total };
}

// Decodes a packed file as a viewer would, and checks it against what went in: every face's corners, and that each face
// comes from (for a preview: shares a corner with) the source face its origin says
function check(glb, checks, exact) {
  const { json, bin } = readGlb(glb);
  const decoded = i => {
    const a = json.accessors[i], v = json.bufferViews[a.bufferView], e = v.extensions.EXT_meshopt_compression;
    const out = new Uint8Array(e.count * e.byteStride);
    D.decodeGltfBuffer(out, e.count, e.byteStride, new Uint8Array(bin.buffer, bin.byteOffset + e.byteOffset, e.byteLength), e.mode, e.filter);
    return out;
  };
  let p = 0;
  for (const m of json.meshes) for (const prim of m.primitives) {
    const c = checks[p++], got = new Uint32Array(decoded(prim.indices).buffer), faces = c.faces;
    if (got.length !== faces.length) throw new Error('faces were lost in packing');
    for (let j = 0; j < faces.length; j += 3) {     // the index codec may start a face at another of its corners
      const [a, b, d] = faces.subarray(j, j + 3), [x, y, z] = got.subarray(j, j + 3);
      if (!(x === a && y === b && z === d || x === b && y === d && z === a || x === d && y === a && z === b)) throw new Error('faces changed in packing');
    }
    for (const [name, r] of Object.entries(c.shown)) {
      const got = decoded(prim.attributes[name]), want = r.kind === 'float' ? new Uint8Array(new Float32Array(r.shown).buffer) : new Uint8Array(r.shown.buffer);
      if (got.length !== want.length || got.some((x, i) => x !== want[i])) throw new Error(`${name} changed in packing`);
    }
    const P = c.prim.attributes.POSITION.values, src = c.prim.faces, pos = c.shown.POSITION.shown, tol = c.prim.scale * 2 ** -12;
    const near = (v, s) => Math.abs(pos[3 * v] - P[3 * s]) <= tol && Math.abs(pos[3 * v + 1] - P[3 * s + 1]) <= tol && Math.abs(pos[3 * v + 2] - P[3 * s + 2]) <= tol;
    for (let j = 0; j < faces.length / 3; j++) {
      const o = c.origins[j];
      const fits = exact ? [0, 1, 2].every(k => near(faces[3 * j + k], src[3 * o + k])) : [0, 1, 2].some(k => near(faces[3 * j], src[3 * o + k]));
      if (!fits) throw new Error(`face ${j} isn't where its source face ${o} was`);
    }
  }
}

async function main() {
  const [path, outDir, previewArg, previewFromArg] = process.argv.slice(2);
  if (!path || !outDir) throw new Error('usage: glbpack.mjs <source.glb> <out dir> [preview faces] [make a preview from faces]');
  await Promise.all([E.ready, S.ready, D.ready]);
  const started = performance.now();
  const src = readGlb(fs.readFileSync(path)), { json } = src;
  const used = [...(json.extensionsUsed || []), ...(json.extensionsRequired || [])];
  if (used.some(x => UNSUPPORTED.includes(x))) throw new Error(`already compressed or quantized (${used.join(', ')})`);
  if (json.skins?.length || json.animations?.length) throw new Error('animated meshes aren\'t supported');
  if (json.images?.some(i => i.bufferView !== undefined)) throw new Error('images inside the GLB aren\'t supported');
  src.prims = {};
  let offset = 0;
  (json.meshes || []).forEach((m, mi) => m.primitives.forEach((p, pi) => {
    if ((p.mode ?? 4) !== 4 || p.indices === undefined || p.targets) throw new Error('only indexed triangles are supported');
    const attributes = {};
    for (const [name, i] of Object.entries(p.attributes)) attributes[name] = accessor(src, i);
    const idx = accessor(src, p.indices), faces = Uint32Array.from(idx.values);
    const P = attributes.POSITION;
    if (!P) throw new Error('a primitive has no positions');
    let scale = 0;
    for (let i = 0; i < P.values.length; i++) scale = Math.max(scale, Math.abs(P.values[i]));
    src.prims[`${mi}/${pi}`] = { attributes, faces, vertices: P.count, offset, scale: scale || 1 };
    offset += faces.length / 3;
  }));
  if (!offset) throw new Error('no faces');
  const made = {}, previewFaces = previewArg === undefined ? 150000 : +previewArg;
  const previewFrom = previewFromArg === undefined ? 2 * previewFaces : +previewFromArg;
  const write = (lod, r) => {
    fs.writeFileSync(`${outDir}/${lod}.glb`, r.glb);
    fs.writeFileSync(`${outDir}/${lod}.faces.bin`, new Uint8Array(r.origins.buffer));
    made[lod] = { bytes: r.glb.length, faces: r.faces };
  };
  const full = pack(src, prim => ({ faces: prim.faces, origin: Uint32Array.from({ length: prim.faces.length / 3 }, (_, i) => i) }), 'full');
  check(full.glb, full.checks, true);
  write('full', full);
  if (previewFaces > 0 && offset >= previewFrom) {
    const share = previewFaces / offset;
    const preview = pack(src, prim => {
      const target = Math.max(3, Math.floor(prim.faces.length * share / 3) * 3);
      const [faces] = S.simplify(prim.faces, prim.attributes.POSITION.values, 3, target, PREVIEW_ERROR, []);
      const corner = new Uint32Array(prim.vertices);      // for each vertex, a face it's a corner of
      for (let i = 0; i < prim.faces.length; i++) corner[prim.faces[i]] = Math.floor(i / 3);
      return { faces, origin: Uint32Array.from({ length: faces.length / 3 }, (_, j) => corner[faces[3 * j]]) };
    }, 'preview');
    check(preview.glb, preview.checks, false);
    write('preview', preview);
  }
  made.source_faces = offset;
  made.seconds = Math.round((performance.now() - started) / 100) / 10;
  console.log(JSON.stringify(made));
}

main().catch(e => { console.error(e.message || String(e)); process.exit(1); });
