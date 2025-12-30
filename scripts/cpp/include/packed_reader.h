#pragma once

#include <cstdint>
#include <cstring>
#include <string>
#include <string_view>
#include <vector>
#include <unordered_map>
#include <optional>
#include <stdexcept>
#include <filesystem>
#include <fstream>
#include <algorithm>
#include <list>
#include <utility>
#include <system_error>

#include "binary_formats.h" // Uuid128 helpers live here
#include "json_utils.h"

namespace metais {

    namespace fs = std::filesystem;

    template <class Key, class Value, class Hash = std::hash<Key>, class Eq = std::equal_to<Key>>
    class LruCache {
    public:
        // capacity == 0 => disabled (never store)
        // capacity == SIZE_MAX => unlimited
        explicit LruCache(std::size_t capacity = 0)
            : capacity_(capacity) {}

        void set_capacity(std::size_t cap) {
            capacity_ = cap;
            evict_if_needed();
        }

        std::size_t capacity() const { return capacity_; }
        std::size_t size() const { return map_.size(); }

        void clear() {
            map_.clear();
            lru_.clear();
        }

        bool get(const Key& k, Value& out) {
            auto it = map_.find(k);
            if (it == map_.end()) return false;
            // move to front
            lru_.splice(lru_.begin(), lru_, it->second);
            out = it->second->value;
            return true;
        }

        void put(const Key& k, const Value& v) {
            if (capacity_ == 0) return; // disabled
            auto it = map_.find(k);
            if (it != map_.end()) {
                it->second->value = v;
                lru_.splice(lru_.begin(), lru_, it->second);
                return;
            }
            lru_.push_front(Node{k, v});
            map_[lru_.front().key] = lru_.begin();
            evict_if_needed();
        }

    private:
        struct Node {
            Key key;
            Value value;
        };

        void evict_if_needed() {
            if (capacity_ == static_cast<std::size_t>(-1)) return; // unlimited
            while (map_.size() > capacity_) {
                auto last = std::prev(lru_.end());
                map_.erase(last->key);
                lru_.pop_back();
            }
        }

        std::size_t capacity_;
        std::list<Node> lru_;
        std::unordered_map<Key, typename std::list<Node>::iterator, Hash, Eq> map_;
    };

    // ----------------------------
    // Tiny file helpers
    // ----------------------------
    inline std::uint64_t file_size_u64(const fs::path& p) {
        std::error_code ec;
        auto s = fs::file_size(p, ec);
        if (ec) throw std::runtime_error("file_size failed: " + p.string() + ": " + ec.message());
        return static_cast<std::uint64_t>(s);
    }

    // ----------------------------
    // Dict reader (lazy by offsets)
    // ----------------------------
    class DictReader {
    public:
        void open(const fs::path& dict_dir) {
            dict_bin_path_     = dict_dir / "dict.bin";
            dict_offsets_path_ = dict_dir / "dict.offsets.bin";

            dict_bin_.open(dict_bin_path_, std::ios::binary);
            if (!dict_bin_) throw std::runtime_error("open failed: " + dict_bin_path_.string());

            offsets_.open(dict_offsets_path_, std::ios::binary);
            if (!offsets_) throw std::runtime_error("open failed: " + dict_offsets_path_.string());

            const std::uint64_t sz = file_size_u64(dict_offsets_path_);
            if (sz % kU64Bytes != 0) throw std::runtime_error("dict.offsets.bin not multiple of " + std::to_string(kU64Bytes) + ": " + dict_offsets_path_.string());
            value_count_plus1_ = sz / kU64Bytes;
            if (value_count_plus1_ == 0) throw std::runtime_error("dict.offsets.bin empty: " + dict_offsets_path_.string());
        }

        std::uint64_t value_count() const {
            return (value_count_plus1_ > 0) ? (value_count_plus1_ - 1) : 0;
        }

        // Returns the raw JSON-encoded bytes (because you wrote json.dump() bytes).
        // You can json::parse(...) it if you want the decoded value.
        std::string read_raw_json(DictIndex idx) {
            if (value_count_plus1_ == 0) throw std::runtime_error("DictReader not opened");
            const std::uint64_t i = static_cast<std::uint64_t>(idx);
            if (i + 1 >= value_count_plus1_) throw std::runtime_error("dict index out of range");

            const std::uint64_t off0 = read_offset(i);
            const std::uint64_t off1 = read_offset(i + 1);
            if (off1 < off0) throw std::runtime_error("dict offsets not monotonic");

            const std::uint64_t n = off1 - off0;
            std::string s;
            s.resize(static_cast<std::size_t>(n));
            seek_or_throw(dict_bin_, off0, dict_bin_path_);
            dict_bin_.read(s.data(), static_cast<std::streamsize>(n));
            if (!dict_bin_) throw std::runtime_error("dict.bin read failed");
            return s;
        }

        // Convenience: parse JSON and return string-ish representation.
        // For non-string JSON values (numbers/bools/null/arrays/objects), this returns the dump.
        std::string to_pretty(DictIndex idx) {
            auto raw = read_raw_json(idx);
            json j = json::parse(raw);
            if (j.is_string()) return j.get<std::string>();
            return j.dump();
        }

    private:
        std::uint64_t read_offset(std::uint64_t i) {
            // offsets are u64 LE
            const std::uint64_t pos = i * kU64Bytes;
            seek_or_throw(offsets_, pos, dict_offsets_path_);
            return read_u64_le(offsets_);
        }

        fs::path dict_bin_path_;
        fs::path dict_offsets_path_;
        std::ifstream dict_bin_;
        std::ifstream offsets_;
        std::uint64_t value_count_plus1_ = 0;
    };

    // ----------------------------
    // Packed reader core
    // ----------------------------
    class PackedReader {
    public:
        struct EntityRef {
            GlobalId global_id = 0;
            CitypeIndex citype_index = 0;
            LocalIndex local_index = 0;
            std::string citype; // resolved name
        };

        struct NodeFormat {
            std::string attributeLayout; // "grid" or "sparse"
            std::uint32_t attributeCount = 0;
            std::uint32_t metaAttributeCount = 0; // should be 6
            std::uint32_t sparseEntryByteSize = 0; // 6 if sparse
        };

        struct GridRow {
            std::vector<std::int32_t> cells; // dict indices (or -1)
        };

        struct SparseRow {
            // pairs (attrIndex -> dictIndex)
            std::vector<std::pair<AttrIndex, DictIndex>> entries;
        };

    public:
        struct CacheConfig {
            std::size_t dict_pretty_lru = 8192;   // DictIndex -> pretty string
            std::size_t uuid_to_gid_lru = 8192;   // Uuid128 -> GlobalId
            std::size_t gid_to_entity_lru = 8192; // GlobalId -> EntityRef
        };

        PackedReader() = default;

        explicit PackedReader(const fs::path& packed_root) {
            open(packed_root);
        }

        explicit PackedReader(const fs::path& packed_root, CacheConfig cc) {
            cache_cfg_ = cc;
            open(packed_root);
            init_caches();
        }

        void open(const fs::path& packed_root) {
            root_      = packed_root;
            dict_root_ = root_ / "dict";
            uuid_root_ = root_ / "uuids";
            node_root_ = root_ / "nodes";
            rel_root_  = root_ / "relations";

            // basic sanity
            if (!fs::exists(dict_root_)) throw std::runtime_error("missing dict/: " + dict_root_.string());
            if (!fs::exists(uuid_root_)) throw std::runtime_error("missing uuids/: " + uuid_root_.string());
            if (!fs::exists(node_root_)) throw std::runtime_error("missing nodes/: " + node_root_.string());
            if (!fs::exists(rel_root_))  throw std::runtime_error("missing relations/: " + rel_root_.string());

            // load manifests (nice for UI/debug, and you asked for it)
            load_manifests();

            // load citypes index list (prefer uuids/citypes.json, fallback to nodes manifest)
            load_citypes_index();

            // open dict
            dict_.open(dict_root_);

            // open global uuid tables
            uuids_bin_path_    = uuid_root_ / "uuids.bin";
            resolver_bin_path_ = uuid_root_ / "resolver.bin";

            uuids_bin_.open(uuids_bin_path_, std::ios::binary);
            if (!uuids_bin_) throw std::runtime_error("open failed: " + uuids_bin_path_.string());

            resolver_bin_.open(resolver_bin_path_, std::ios::binary);
            if (!resolver_bin_) throw std::runtime_error("open failed: " + resolver_bin_path_.string());

            // compute N nodes from uuids.bin size
            const std::uint64_t sz = file_size_u64(uuids_bin_path_);
            if (sz % kUuidBytes != 0) throw std::runtime_error("uuids.bin not multiple of " + std::to_string(kUuidBytes) + ": " + uuids_bin_path_.string());
            global_node_count_ = sz / kUuidBytes;

            // resolver.bin rows are (U16 citype_index + U32 local_index) = 6 bytes per row
            const std::uint64_t rsz = file_size_u64(resolver_bin_path_);
            if (rsz != global_node_count_ * kResolverRowBytes) {
                throw std::runtime_error(
                    "resolver.bin size mismatch: got " + std::to_string(rsz) +
                    ", expected " + std::to_string(global_node_count_ * kResolverRowBytes) + " (" +
                    std::to_string(global_node_count_) + " rows)");
            }

            init_caches();
        }

        const fs::path& root() const { return root_; }

        // --- manifests ---
        const std::vector<std::string>& citypes_manifest() const { return citypes_manifest_; }
        const std::vector<std::string>& reltypes_manifest() const { return reltypes_manifest_; }

        // --- dict ---
        DictReader& dict() { return dict_; }
        const DictReader& dict() const { return dict_; }
        std::string dict_pretty_cached(DictIndex idx) {
            std::string out;
            if (dict_pretty_cache_.get(idx, out)) return out;
            out = dict_.to_pretty(idx);     // does file reads + json::parse
            dict_pretty_cache_.put(idx, out);
            return out;
        }

        // --- global node count ---
        std::uint64_t global_node_count() const { return global_node_count_; }

        // --- uuid -> global id ---
        std::optional<GlobalId> find_global_id(const Uuid128& u) {
            GlobalId cached;
            if (uuid_gid_cache_.get(u, cached)) return cached;

            if (!uuids_bin_) throw std::runtime_error("uuids.bin not open");
            const std::uint64_t n = global_node_count_;

            std::uint64_t lo = 0;
            std::uint64_t hi = n; // [lo, hi)

            while (lo < hi) {
                const std::uint64_t mid = lo + (hi - lo) / 2;

                const std::uint64_t pos = mid * kUuidBytes;
                Uuid128 m = read_uuid16_at(uuids_bin_, uuids_bin_path_, pos);

                if (m < u) lo = mid + 1;
                else       hi = mid;
            }

            if (lo >= n) return std::nullopt;

            uuids_bin_.clear();
            Uuid128 m = read_uuid16_at(uuids_bin_, uuids_bin_path_, lo * kUuidBytes);
            if (!(m == u)) return std::nullopt;

            GlobalId gid = static_cast<GlobalId>(lo);
            uuid_gid_cache_.put(u, gid);
            return gid;
        }

        // --- global id -> (citype, local_index) ---
        EntityRef get_entity(GlobalId gid) {
            if (gid >= global_node_count_) throw std::runtime_error("global id out of range");

            // first check it's cached
            EntityRef cached;
            if (gid_entity_cache_.get(gid, cached)) return cached;

            const std::uint64_t pos = static_cast<std::uint64_t>(gid) * kResolverRowBytes;

            // layout: U16 citype_index, then U32 local_index (both LE)
            CitypeIndex ci = read_u16_le_at(resolver_bin_, resolver_bin_path_, pos);
            LocalIndex  li = read_u32_le_at(resolver_bin_, resolver_bin_path_, pos + kU16Bytes);

            EntityRef e;
            e.global_id = gid;
            e.citype_index = ci;
            e.local_index = li;
            e.citype = citype_name(ci);
            gid_entity_cache_.put(gid, e);
            return e;
        }

        std::string citype_name(CitypeIndex idx) const {
            if (idx >= citypes_index_.size()) {
                return std::string("<?citype_index=") + std::to_string(idx) + ">";
            }
            return citypes_index_[idx];
        }

        // ----------------------------
        // Node reading (attributes/meta)
        // ----------------------------
        NodeFormat load_node_format(const std::string& citype) {
            const fs::path fmtp = node_root_ / citype / "format.json";
            json j = load_json_file(fmtp);

            NodeFormat f;
            f.attributeLayout = j.value("attributeLayout", "grid");
            f.attributeCount = j.value("attributeCount", 0u);
            f.metaAttributeCount = j.value("metaAttributeCount", 0u);
            f.sparseEntryByteSize = j.value("sparseEntryByteSize", 0u);
            if (f.attributeCount == 0 && f.attributeLayout != "grid") {
                // allow empty-but-sparse? probably not, but keep soft for now
            }
            return f;
        }

        // Grid attributes row: returns A int32 cells (dict indices or -1)
        GridRow read_node_attributes_grid(const std::string& citype, LocalIndex local_index) {
            NodeFormat f = load_node_format(citype);
            if (f.attributeLayout != "grid") throw std::runtime_error("citype not in grid layout: " + citype);

            const fs::path p = node_root_ / citype / "attributes.bin";
            std::ifstream is(p, std::ios::binary);
            if (!is) throw std::runtime_error("open failed: " + p.string());

            const std::uint64_t A = f.attributeCount;
            const std::uint64_t row_bytes = A * kI32Bytes;
            const std::uint64_t pos = static_cast<std::uint64_t>(local_index) * row_bytes;

            if (pos + row_bytes > file_size_u64(p)) {
                throw std::runtime_error("attributes.bin read out of range for " + citype);
            }

            GridRow row;
            row.cells.resize(static_cast<std::size_t>(A));
            seek_or_throw(is, pos, p);
            for (std::uint64_t i = 0; i < A; ++i) row.cells[(std::size_t)i] = read_i32_le(is);
            return row;
        }

        // Sparse attributes row: reads (attrIndex U16, dictIndex U32) entries
        SparseRow read_node_attributes_sparse(const std::string& citype, LocalIndex local_index) {
            NodeFormat f = load_node_format(citype);
            if (f.attributeLayout != "sparse") throw std::runtime_error("citype not in sparse layout: " + citype);
            if (f.sparseEntryByteSize != kSparseAttrEntryBytes) throw std::runtime_error("unexpected sparseEntryByteSize for " + citype);

            const fs::path offp = node_root_ / citype / "attribute_offsets.bin";
            const fs::path atp  = node_root_ / citype / "attributes.bin";

            std::ifstream off(offp, std::ios::binary);
            if (!off) throw std::runtime_error("open failed: " + offp.string());
            std::ifstream at(atp, std::ios::binary);
            if (!at) throw std::runtime_error("open failed: " + atp.string());

            const std::uint64_t offs_sz = file_size_u64(offp);
            if (offs_sz % kU32Bytes != 0) throw std::runtime_error("attribute_offsets.bin not u32 array: " + offp.string());
            const std::uint64_t n_plus1 = offs_sz / kU32Bytes;
            if (static_cast<std::uint64_t>(local_index) + 1 >= n_plus1) {
                throw std::runtime_error("local_index out of range for attribute_offsets.bin: " + citype);
            }

            const std::uint64_t pos0 = static_cast<std::uint64_t>(local_index) * kU32Bytes;
            const std::uint32_t o0 = read_u32_le_at(off, offp, pos0);
            const std::uint32_t o1 = read_u32_le_at(off, offp, pos0 + kU32Bytes);

            if (o1 < o0) throw std::runtime_error("attribute offsets not monotonic: " + citype);

            const std::uint64_t bytes = static_cast<std::uint64_t>(o1 - o0);
            if (bytes % kSparseAttrEntryBytes != 0) throw std::runtime_error("sparse row bytes not multiple of " + std::to_string(kSparseAttrEntryBytes) + ": " + citype);

            SparseRow row;
            const std::size_t n = static_cast<std::size_t>(bytes / kSparseAttrEntryBytes);

            seek_or_throw(at, o0, atp);
            row.entries.reserve(n);
            for (std::size_t i = 0; i < n; ++i) {
                AttrIndex a = static_cast<AttrIndex>(read_u16_le(at));
                DictIndex d = static_cast<DictIndex>(read_u32_le(at));
                row.entries.emplace_back(a, d);
            }
            return row;
        }

        // Meta is always grid: 6 int32 values
        GridRow read_node_meta_grid(const std::string& citype, LocalIndex local_index) {
            const fs::path p = node_root_ / citype / "metaAttributes.bin";
            std::ifstream is(p, std::ios::binary);
            if (!is) throw std::runtime_error("open failed: " + p.string());

            constexpr std::uint64_t M = kMetaAttrCount;
            const std::uint64_t row_bytes = M * kI32Bytes;
            const std::uint64_t pos = static_cast<std::uint64_t>(local_index) * row_bytes;

            if (pos + row_bytes > file_size_u64(p)) {
                throw std::runtime_error("metaAttributes.bin read out of range for " + citype);
            }

            GridRow row;
            row.cells.resize((std::size_t)M);
            seek_or_throw(is, pos, p);
            for (std::uint64_t i = 0; i < M; ++i) row.cells[(std::size_t)i] = read_i32_le(is);
            return row;
        }

        // Convenience: from uuid -> entity -> read meta/attrs
        // (You can decide later how you want to represent results.)
        struct NodeView {
            EntityRef ref;
            NodeFormat fmt;
            std::optional<GridRow> attrs_grid;
            std::optional<SparseRow> attrs_sparse;
            GridRow meta; // always present
        };

        std::optional<NodeView> get_node_by_uuid(std::string_view uuid_str) {
            auto gid = find_global_id(uuid_str);
            if (!gid) return std::nullopt;
            return get_node_by_global_id(*gid);
        }

        std::optional<NodeView> get_node_by_global_id(GlobalId gid) {
            EntityRef e = get_entity(gid);
            NodeView v;
            v.ref = e;
            v.fmt = load_node_format(e.citype);
            v.meta = read_node_meta_grid(e.citype, e.local_index);

            if (v.fmt.attributeLayout == "grid") {
                // attributes.bin may be absent for "no attributes" citypes; keep soft.
                const fs::path ap = node_root_ / e.citype / "attributes.bin";
                if (fs::exists(ap)) v.attrs_grid = read_node_attributes_grid(e.citype, e.local_index);
            } else if (v.fmt.attributeLayout == "sparse") {
                const fs::path ap = node_root_ / e.citype / "attributes.bin";
                const fs::path op = node_root_ / e.citype / "attribute_offsets.bin";
                if (fs::exists(ap) && fs::exists(op)) v.attrs_sparse = read_node_attributes_sparse(e.citype, e.local_index);
            } else {
                throw std::runtime_error("unknown attributeLayout in format.json for " + e.citype + ": " + v.fmt.attributeLayout);
            }

            return v;
        }

    private:
        void init_caches() {
            dict_pretty_cache_.set_capacity(cache_cfg_.dict_pretty_lru);
            uuid_gid_cache_.set_capacity(cache_cfg_.uuid_to_gid_lru);
            gid_entity_cache_.set_capacity(cache_cfg_.gid_to_entity_lru);
        }

        void load_manifests() {
            // nodes/citypes_manifest.json
            {
                const fs::path p = node_root_ / "citypes_manifest.json";
                if (fs::exists(p)) {
                    json j = load_json_file(p);
                    if (j.contains("citypes") && j["citypes"].is_array()) {
                        citypes_manifest_.clear();
                        for (auto& x : j["citypes"]) citypes_manifest_.push_back(x.get<std::string>());
                    }
                }
            }
            // relations/reltypes_manifest.json
            {
                const fs::path p = rel_root_ / "reltypes_manifest.json";
                if (fs::exists(p)) {
                    json j = load_json_file(p);
                    if (j.contains("reltypes") && j["reltypes"].is_array()) {
                        reltypes_manifest_.clear();
                        for (auto& x : j["reltypes"]) reltypes_manifest_.push_back(x.get<std::string>());
                    }
                }
            }
        }

        void load_citypes_index() {
            // Prefer uuids/citypes.json (spec), fallback to nodes manifest if that’s what exists.
            const fs::path p1 = uuid_root_ / "citypes.json";
            if (fs::exists(p1)) {
                json j = load_json_file(p1);
                if (!j.is_array()) throw std::runtime_error("uuids/citypes.json must be array");
                citypes_index_.clear();
                for (auto& x : j) citypes_index_.push_back(x.get<std::string>());
                return;
            }

            if (!citypes_manifest_.empty()) {
                // Use manifest ordering as index ordering (better than nothing)
                citypes_index_ = citypes_manifest_;
                return;
            }

            throw std::runtime_error("could not load citype index: missing uuids/citypes.json and nodes/citypes_manifest.json");
        }

    private:
        fs::path root_;
        fs::path dict_root_;
        fs::path uuid_root_;
        fs::path node_root_;
        fs::path rel_root_;

        // manifests
        std::vector<std::string> citypes_manifest_;
        std::vector<std::string> reltypes_manifest_;

        // citype index list (CitypeIndex -> name)
        std::vector<std::string> citypes_index_;

        // dict
        DictReader dict_;

        // global uuid tables
        fs::path uuids_bin_path_;
        fs::path resolver_bin_path_;
        std::ifstream uuids_bin_;
        std::ifstream resolver_bin_;
        std::uint64_t global_node_count_ = 0;

        CacheConfig cache_cfg_{};

        LruCache<DictIndex, std::string> dict_pretty_cache_{0};
        LruCache<Uuid128, GlobalId, Uuid128Hash> uuid_gid_cache_{0};
        LruCache<GlobalId, EntityRef> gid_entity_cache_{0};
    };

}