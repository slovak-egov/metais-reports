// packedStore.js
// Browser-side helper for reading repacked MetaIS datasets.
//
// Usage:
//   import { createPackedStore } from './packedStore.js';
//
//   const store = await createPackedStore('/data/repack_project'); // e.g.
//   const types = store.listTypes();          // ['PO','AS','KS','ISVS','Projekt']
//   const projekt = await store.openType('Projekt');
//   for await (const [uuid, attrs] of projekt.iterRecords(['Gen_Profil_nazov'])) {
//     console.log(uuid, attrs.Gen_Profil_nazov);
//   }

const TEXT_DECODER = new TextDecoder('utf-8');

// ---------------------------------------------------------
// Small fetch helpers
// ---------------------------------------------------------

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch JSON: ${url} (${res.status})`);
  return res.json();
}

async function fetchArrayBuffer(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch binary: ${url} (${res.status})`);
  return res.arrayBuffer();
}

// ---------------------------------------------------------
// StreamingGlobalDictJS (but all in memory)
// ---------------------------------------------------------

class StreamingGlobalDictJS {
  constructor(valuesBuffer, offsetsBuffer, meta) {
    this.valueCount = Number(meta.valueCount);
    this.valuesBuffer = valuesBuffer;
    this.offsetsBuffer = offsetsBuffer;
    this.offsetView = new DataView(offsetsBuffer);
  }

  _readOffset(idx) {
    if (idx < 0 || idx > this.valueCount) {
      throw new RangeError(`offset index out of range: ${idx}`);
    }
    // uint64 little endian → we assume offsets fit into JS Number
    const lo = this.offsetView.getUint32(idx * 8, true);
    const hi = this.offsetView.getUint32(idx * 8 + 4, true);
    return hi * 2 ** 32 + lo;
  }

  get(idx) {
    if (idx < 0 || idx >= this.valueCount) {
      throw new RangeError(`dict index out of range: ${idx}`);
    }
    const start = this._readOffset(idx);
    const end   = this._readOffset(idx + 1);
    const length = end - start;
    if (length < 0) {
      throw new Error(`Negative length for idx ${idx}: ${length}`);
    }

    const bytes = new Uint8Array(this.valuesBuffer, start, length);
    const s = TEXT_DECODER.decode(bytes);
    return JSON.parse(s);
  }
}

// ---------------------------------------------------------
// UuidIndexJS
// ---------------------------------------------------------

class UuidIndexJS {
  constructor(uuidsBuffer, meta) {
    this.recordCount = Number(meta.recordCount);
    this.uuidBytes   = Number(meta.uuidBytes || 16);
    if (this.uuidBytes !== 16) {
      throw new Error('UuidIndexJS only supports 16-byte UUIDs');
    }
    this.buffer = uuidsBuffer;
    this.view   = new DataView(uuidsBuffer);
  }

  _readUuidBytes(idx) {
    if (idx < 0 || idx >= this.recordCount) {
      throw new RangeError('uuid index out of range');
    }
    const start = idx * this.uuidBytes;
    return new Uint8Array(this.buffer, start, this.uuidBytes);
  }

  _bytesToUuidString(bytes) {
    // Convert 16 bytes → canonical UUID string
    let hex = '';
    for (let i = 0; i < bytes.length; i++) {
      const b = bytes[i].toString(16).padStart(2, '0');
      hex += b;
    }
    return (
      hex.slice(0, 8) + '-' +
      hex.slice(8, 12) + '-' +
      hex.slice(12, 16) + '-' +
      hex.slice(16, 20) + '-' +
      hex.slice(20)
    );
  }

  getUuid(id) {
    const bytes = this._readUuidBytes(id);
    return this._bytesToUuidString(bytes);
  }

  getId(uuidStr) {
    // Parse uuidStr to 16 bytes
    const normalized = uuidStr.replace(/-/g, '').toLowerCase();
    if (normalized.length !== 32) return null;
    const target = new Uint8Array(16);
    for (let i = 0; i < 16; i++) {
      target[i] = parseInt(normalized.slice(i * 2, i * 2 + 2), 16);
    }

    // Binary search over sorted uuids.bin
    let lo = 0;
    let hi = this.recordCount - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const midBytes = this._readUuidBytes(mid);

      let cmp = 0;
      for (let i = 0; i < 16; i++) {
        const a = midBytes[i];
        const b = target[i];
        if (a < b) { cmp = -1; break; }
        if (a > b) { cmp = 1;  break; }
      }

      if (cmp === 0) return mid;
      if (cmp < 0) lo = mid + 1;
      else         hi = mid - 1;
    }
    return null;
  }
}

// ---------------------------------------------------------
// UuidTypeIndexJS
// ---------------------------------------------------------

class UuidTypeIndexJS {
  constructor(typesBuffer, meta) {
    this.recordCount   = Number(meta.recordCount);
    this.bytesPerCode  = Number(meta.bytesPerCode || 2);
    this.endianness    = meta.endianness || 'LE';
    if (this.endianness !== 'LE') {
      throw new Error('UuidTypeIndexJS only supports little-endian');
    }

    this.codeToType = new Map();
    this.typeToCode = new Map();
    for (const entry of meta.types || []) {
      const code = Number(entry.code);
      const t    = entry.typeName;
      this.codeToType.set(code, t);
      this.typeToCode.set(t, code);
    }

    this.buffer = typesBuffer;
    this.view   = new DataView(typesBuffer);
  }

  _readCodeAt(idx) {
    if (idx < 0 || idx >= this.recordCount) {
      throw new RangeError('uuid type index out of range');
    }
    const offset = idx * this.bytesPerCode;
    if (this.bytesPerCode === 1) {
      return this.view.getUint8(offset);
    } else if (this.bytesPerCode === 2) {
      return this.view.getUint16(offset, true);
    } else if (this.bytesPerCode === 4) {
      return this.view.getUint32(offset, true);
    } else {
      throw new Error(`Unsupported bytesPerCode: ${this.bytesPerCode}`);
    }
  }

  getTypeById(id) {
    const code = this._readCodeAt(id);
    return this.codeToType.get(code) || null;
  }

  getCodeForType(typeName) {
    return this.typeToCode.get(typeName) ?? null;
  }
}

// ---------------------------------------------------------
// TypeViewJS
// ---------------------------------------------------------

class TypeViewJS {
  constructor(typeName, baseUrl, globalDict, meta) {
    this.typeName   = typeName;
    this.baseUrl    = baseUrl;
    this.globalDict = globalDict;

    this.recordCount = Number(meta.recordCount);
    this.blockSize   = Number(meta.blockSize);
    this.intBytes    = Number(meta.intBytes);
    this.endianness  = meta.endianness || 'LE';
    this.missing     = Number(meta.missingSentinel);

    if (this.intBytes !== 4 || this.endianness !== 'LE') {
      throw new Error(`TypeViewJS(${typeName}) only supports intBytes=4, LE`);
    }

    // Parse attributes like Python
    const rawAttrs = meta.attributes || [];
    this.attributes = [];
    this.attrMeta   = {};

    if (rawAttrs.length && Array.isArray(rawAttrs[0])) {
      // [[tech, human, desc], ...]
      for (const triple of rawAttrs) {
        const tech = triple[0];
        if (!tech) continue;
        this.attributes.push(tech);
        this.attrMeta[tech] = {
          name: triple[1] ?? null,
          description: triple[2] ?? null,
        };
      }
    } else if (rawAttrs.length && typeof rawAttrs[0] === 'object') {
      // [{technicalName, name, description}, ...]
      for (const item of rawAttrs) {
        const tech = item.technicalName || item.name;
        if (!tech) continue;
        this.attributes.push(tech);
        this.attrMeta[tech] = {
          name: item.name ?? null,
          description: item.description ?? null,
        };
      }
    } else {
      // ["Gen_Profil_nazov", ...]
      for (const name of rawAttrs) {
        this.attributes.push(name);
        this.attrMeta[name] = { name: null, description: null };
      }
    }

    this.attrIndex = new Map(this.attributes.map((name, idx) => [name, idx]));

    this._binUrl   = `${baseUrl}/nodes/${typeName}.bin`;
    this._uuidsUrl = `${baseUrl}/nodes/${typeName}.uuids.bin`;

    this._binBuffer   = null;
    this._binView     = null;
    this._uuidsBuffer = null;
    this._uuidsView   = null;
  }

  async _ensureLoaded() {
    if (!this._binBuffer) {
      this._binBuffer = await fetchArrayBuffer(this._binUrl);
      this._binView   = new DataView(this._binBuffer);
    }
    if (!this._uuidsBuffer) {
      this._uuidsBuffer = await fetchArrayBuffer(this._uuidsUrl);
      this._uuidsView   = new DataView(this._uuidsBuffer);
    }
  }

  listAttributes() {
    return [...this.attributes];
  }

  // ---- UUID helpers ----

  async getUuid(recordIdx) {
    await this._ensureLoaded();
    if (recordIdx < 0 || recordIdx >= this.recordCount) {
      throw new RangeError('record index out of range');
    }
    const start = recordIdx * 16;
    const bytes = new Uint8Array(this._uuidsBuffer, start, 16);
    // Reuse logic from UuidIndexJS
    let hex = '';
    for (let i = 0; i < 16; i++) {
      hex += bytes[i].toString(16).padStart(2, '0');
    }
    return (
      hex.slice(0, 8) + '-' +
      hex.slice(8, 12) + '-' +
      hex.slice(12, 16) + '-' +
      hex.slice(16, 20) + '-' +
      hex.slice(20)
    );
  }

  async findRecordIndexByUuid(uuidStr) {
    await this._ensureLoaded();
    const normalized = uuidStr.replace(/-/g, '').toLowerCase();
    if (normalized.length !== 32) return null;
    const target = new Uint8Array(16);
    for (let i = 0; i < 16; i++) {
      target[i] = parseInt(normalized.slice(i * 2, i * 2 + 2), 16);
    }

    let lo = 0;
    let hi = this.recordCount - 1;

    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const start = mid * 16;
      const bytes = new Uint8Array(this._uuidsBuffer, start, 16);

      let cmp = 0;
      for (let i = 0; i < 16; i++) {
        const a = bytes[i];
        const b = target[i];
        if (a < b) { cmp = -1; break; }
        if (a > b) { cmp = 1;  break; }
      }

      if (cmp === 0) return mid;
      if (cmp < 0) lo = mid + 1;
      else         hi = mid - 1;
    }

    return null;
  }

  // ---- attribute helpers ----

  _readIntAt(recordIdx, colIdx) {
    if (recordIdx < 0 || recordIdx >= this.recordCount) {
      throw new RangeError('record index out of range');
    }
    if (colIdx < 0 || colIdx >= this.blockSize) {
      throw new RangeError('column index out of range');
    }
    const offset = (recordIdx * this.blockSize + colIdx) * this.intBytes;
    return this._binView.getInt32(offset, true);
  }

  async getAttrIndex(recordIdx, attrName) {
    await this._ensureLoaded();
    const col = this.attrIndex.get(attrName);
    if (col == null) {
      throw new Error(`Attribute not found for type ${this.typeName}: ${attrName}`);
    }
    const val = this._readIntAt(recordIdx, col);
    return val === this.missing ? null : val;
  }

  async getAttrValue(recordIdx, attrName) {
    const dictIdx = await this.getAttrIndex(recordIdx, attrName);
    if (dictIdx == null) return null;
    return this.globalDict.get(dictIdx);
  }

  async getAllNonMissingAttrs(recordIdx) {
    await this._ensureLoaded();
    const res = {};
    const blockBytes = this.blockSize * this.intBytes;
    const offset = recordIdx * blockBytes;
    for (let col = 0; col < this.blockSize; col++) {
      const val = this._binView.getInt32(offset + col * this.intBytes, true);
      if (val === this.missing) continue;
      const name = this.attributes[col];
      res[name] = this.globalDict.get(val);
    }
    return res;
  }

  // ---- sequential iteration ----

  async *iterRecords(attrNames = null) {
    await this._ensureLoaded();

    let cols, colToName;
    if (attrNames == null) {
      cols = [...Array(this.blockSize).keys()];
      colToName = {};
      for (let i = 0; i < this.blockSize; i++) {
        colToName[i] = this.attributes[i];
      }
    } else {
      cols = [];
      colToName = {};
      for (const name of attrNames) {
        const col = this.attrIndex.get(name);
        if (col == null) {
          throw new Error(`Attribute not found for type ${this.typeName}: ${name}`);
        }
        cols.push(col);
        colToName[col] = name;
      }
    }

    const blockBytes = this.blockSize * this.intBytes;

    for (let idx = 0; idx < this.recordCount; idx++) {
      // UUID
      const uuidStart = idx * 16;
      const uuidBytes = new Uint8Array(this._uuidsBuffer, uuidStart, 16);
      let hex = '';
      for (let i = 0; i < 16; i++) {
        hex += uuidBytes[i].toString(16).padStart(2, '0');
      }
      const uuidStr =
        hex.slice(0, 8) + '-' +
        hex.slice(8, 12) + '-' +
        hex.slice(12, 16) + '-' +
        hex.slice(16, 20) + '-' +
        hex.slice(20);

      // Attributes
      const rowOffset = idx * blockBytes;
      const attrs = {};
      for (const col of cols) {
        const val = this._binView.getInt32(rowOffset + col * this.intBytes, true);
        if (val === this.missing) continue;
        const name = colToName[col];
        attrs[name] = this.globalDict.get(val);
      }

      yield [uuidStr, attrs];
    }
  }
}

// ---------------------------------------------------------
// RelationFileJS & RelationStoreJS
// ---------------------------------------------------------

class RelationFileJS {
  constructor(binBuffer, meta) {
    this.buffer = binBuffer;
    this.view   = new DataView(binBuffer);

    this.recordCount = Number(meta.recordCount);
    this.layout      = meta.layout || ['src', 'tgt'];
    this.sortedBy    = meta.sortedBy || this.layout;
    this.technicalName = meta.technicalName || null;
    this.name        = meta.name || null;
    this.description = meta.description || null;

    if (meta.intBytes !== 4 || meta.endianness !== 'LE') {
      throw new Error('RelationFileJS only supports int32 LE');
    }
  }

  _readPairAt(idx) {
    if (idx < 0 || idx >= this.recordCount) {
      throw new RangeError('relation record index out of range');
    }
    const offset = idx * 8;
    const first  = this.view.getInt32(offset, true);
    const second = this.view.getInt32(offset + 4, true);
    return [first, second];
  }

  _lowerBoundFirst(key) {
    let lo = 0;
    let hi = this.recordCount;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const [first] = this._readPairAt(mid);
      if (first < key) lo = mid + 1;
      else             hi = mid;
    }
    return lo;
  }

  _upperBoundFirst(key) {
    let lo = 0;
    let hi = this.recordCount;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const [first] = this._readPairAt(mid);
      if (first <= key) lo = mid + 1;
      else              hi = mid;
    }
    return lo;
  }

  *iterPairsForFirst(firstId) {
    const start = this._lowerBoundFirst(firstId);
    if (start >= this.recordCount) return;
    const end   = this._upperBoundFirst(firstId);
    for (let i = start; i < end; i++) {
      yield this._readPairAt(i);
    }
  }

  hasPair(firstId, secondId) {
    const start = this._lowerBoundFirst(firstId);
    if (start >= this.recordCount) return false;
    const end = this._upperBoundFirst(firstId);
    let lo = start;
    let hi = end - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const [first, second] = this._readPairAt(mid);
      if (second === secondId) return true;
      if (second < secondId) lo = mid + 1;
      else                   hi = mid - 1;
    }
    return false;
  }
}

class RelationStoreJS {
  constructor(baseUrl, uuidIndex, ctypeIndex, relationsMap) {
    this.baseUrl    = baseUrl;
    this.uuidIndex  = uuidIndex;
    this.ctypeIndex = ctypeIndex || {};
    this.relations  = relationsMap; // { reltype: { 'src.tgt': RelationFileJS, 'tgt.src': ... } }
  }

  _uuidToId(uuidStr) {
    return this.uuidIndex.getId(uuidStr);
  }

  _idToUuid(id) {
    try {
      return this.uuidIndex.getUuid(id);
    } catch {
      return null;
    }
  }

  neighborsFrom(reltype, srcUuid) {
    const files = this.relations[reltype];
    if (!files || !files['src.tgt']) return [];
    const srcId = this._uuidToId(srcUuid);
    if (srcId == null) return [];
    const rf = files['src.tgt'];
    const out = [];
    for (const [, tgtId] of rf.iterPairsForFirst(srcId)) {
      const u = this._idToUuid(tgtId);
      if (u) out.push(u);
    }
    return out;
  }

  neighborsTo(reltype, tgtUuid) {
    const files = this.relations[reltype];
    if (!files || !files['tgt.src']) return [];
    const tgtId = this._uuidToId(tgtUuid);
    if (tgtId == null) return [];
    const rf = files['tgt.src'];
    const out = [];
    for (const [, srcId] of rf.iterPairsForFirst(tgtId)) {
      const u = this._idToUuid(srcId);
      if (u) out.push(u);
    }
    return out;
  }

  hasRelationSrcTgt(reltype, srcUuid, tgtUuid) {
    const files = this.relations[reltype];
    if (!files || !files['src.tgt']) return false;
    const srcId = this._uuidToId(srcUuid);
    const tgtId = this._uuidToId(tgtUuid);
    if (srcId == null || tgtId == null) return false;
    return files['src.tgt'].hasPair(srcId, tgtId);
  }

  listRelationsForCtype(ctype, role = 'any') {
    const entry = this.ctypeIndex[ctype];
    if (!entry) return [];
    if (role === 'asSource') return [...(entry.asSource || [])];
    if (role === 'asTarget') return [...(entry.asTarget || [])];
    return [...(entry.asSource || []), ...(entry.asTarget || [])];
  }

  listRelationsBetweenCtypes(c1, c2) {
    const entry = this.ctypeIndex[c1];
    const rels = new Set();
    if (entry) {
      for (const item of entry.asSource || []) {
        if (item.otherType === c2) rels.add(item.reltype);
      }
      for (const item of entry.asTarget || []) {
        if (item.otherType === c2) rels.add(item.reltype);
      }
    }
    return [...rels].sort();
  }
}

// ---------------------------------------------------------
// PackedStoreJS + factory
// ---------------------------------------------------------

export class PackedStoreJS {
  constructor(baseUrl, manifest, globalDict, uuidIndex, uuidTypes, relationStore) {
    this.baseUrl    = baseUrl.replace(/\/+$/, '');
    this.manifest   = manifest;
    this.globalDict = globalDict;
    this.uuidIndex  = uuidIndex;
    this.uuidTypes  = uuidTypes;
    this.relations  = relationStore;
  }

  listTypes() {
    if (Array.isArray(this.manifest.nodeTypes) && this.manifest.nodeTypes.length > 0) {
      return [...this.manifest.nodeTypes].sort();
    }
    // Fallback: could read nodes/index.json, but for repacks manifest should have it.
    return [];
  }

  async openType(typeName) {
    const nodesIndexUrl = `${this.baseUrl}/nodes/${typeName}.meta.json`;
    const meta = await fetchJson(nodesIndexUrl);
    return new TypeViewJS(typeName, this.baseUrl, this.globalDict, meta);
  }

  getCtypeForUuid(uuidStr) {
    const id = this.uuidIndex.getId(uuidStr);
    if (id == null) return null;
    return this.uuidTypes.getTypeById(id);
  }
}

export async function createPackedStore(baseUrl) {
  const root = baseUrl.replace(/\/+$/, '');

  // 1) manifest
  const manifest = await fetchJson(`${root}/manifest.json`);

  // 2) dict
  const dictMeta   = await fetchJson(`${root}/dict/dict.meta.json`);
  const dictValues = await fetchArrayBuffer(`${root}/dict/dict.values.bin`);
  const dictOffsets= await fetchArrayBuffer(`${root}/dict/dict.offsets.bin`);
  const globalDict = new StreamingGlobalDictJS(dictValues, dictOffsets, dictMeta);

  // 3) uuid_index
  const uuidMeta   = await fetchJson(`${root}/uuid_index/meta.json`);
  const uuidsBuf   = await fetchArrayBuffer(`${root}/uuid_index/uuids.bin`);
  const uuidIndex  = new UuidIndexJS(uuidsBuf, uuidMeta);

  // 4) uuid_types
  const uuidTypesMeta = await fetchJson(`${root}/uuid_types/meta.json`);
  const uuidTypesBuf  = await fetchArrayBuffer(`${root}/uuid_types/types.bin`);
  const uuidTypes     = new UuidTypeIndexJS(uuidTypesBuf, uuidTypesMeta);

  // 5) relations: load index + per-reltype binaries
  let ctypeIndex = {};
  try {
    ctypeIndex = await fetchJson(`${root}/relations/index_by_ctype.json`);
  } catch {
    ctypeIndex = {};
  }

  let relIndex = {};
  try {
    relIndex = await fetchJson(`${root}/relations/index.json`);
  } catch {
    relIndex = {};
  }

  const relationsMap = {};
  if (relIndex.relationTypes) {
    for (const relEntry of relIndex.relationTypes) {
      const technicalName = relEntry.technicalName;
      const prefix        = `${root}/relations/${technicalName}`;
      // src.tgt
      const srcMeta = await fetchJson(`${prefix}.src.tgt.meta.json`);
      const srcBuf  = await fetchArrayBuffer(`${prefix}.src.tgt.bin`);
      const rfSrc   = new RelationFileJS(srcBuf, srcMeta);

      // tgt.src
      const tgtMeta = await fetchJson(`${prefix}.tgt.src.meta.json`);
      const tgtBuf  = await fetchArrayBuffer(`${prefix}.tgt.src.bin`);
      const rfTgt   = new RelationFileJS(tgtBuf, tgtMeta);

      relationsMap[technicalName] = {
        'src.tgt': rfSrc,
        'tgt.src': rfTgt,
      };
    }
  }

  const relationStore = new RelationStoreJS(root, uuidIndex, ctypeIndex, relationsMap);
  return new PackedStoreJS(root, manifest, globalDict, uuidIndex, uuidTypes, relationStore);
}