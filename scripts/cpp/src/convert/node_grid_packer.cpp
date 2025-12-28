#include "node_grid_packer.h"
#include "step_marker.h"
#include "json_utils.h"

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace metais {

    NodeGridPacker::NodeGridPacker(const DirectoryLayout& layout, const DictLookup& dict)
        : layout_(layout), dict_(dict) {}

    bool NodeGridPacker::citype_done_marker_exists(const fs::path& citype_dir) {
        return fs::exists(citype_dir / ".pass2.done");
    }

    std::vector<Uuid128> NodeGridPacker::load_uuids(const fs::path& uuids_bin) {
        std::uint64_t bytes = fs::file_size(uuids_bin);
        if (bytes % 16 != 0) throw std::runtime_error("uuids.bin not multiple of 16: " + uuids_bin.string());
        std::size_t n = (std::size_t)(bytes / 16);

        std::vector<Uuid128> v;
        v.reserve(n);
        std::ifstream is(uuids_bin, std::ios::binary);
        if (!is) throw std::runtime_error("open failed: " + uuids_bin.string());
        for (std::size_t i = 0; i < n; ++i) v.push_back(read_uuid_raw16(is));
        return v;
    }

    std::unordered_map<std::string, std::uint32_t>
    NodeGridPacker::load_attr_index(const fs::path& attributes_json) {
        std::ifstream is(attributes_json, std::ios::binary);
        if (!is) throw std::runtime_error("open failed: " + attributes_json.string());
        json arr; is >> arr;
        if (!arr.is_array()) throw std::runtime_error("attributes.json must be array: " + attributes_json.string());

        std::unordered_map<std::string, std::uint32_t> m;
        m.reserve(arr.size() * 2);

        std::uint32_t idx = 0;
        for (const auto& it : arr) {
            if (!it.is_object()) continue;
            if (!it.contains("technicalName") || !it["technicalName"].is_string()) continue;
            m.emplace(it["technicalName"].get<std::string>(), idx++);
        }
        return m;
    }

    void NodeGridPacker::prefill_file_with_ff(const fs::path& tmp_path, std::uint64_t bytes) {
        fs::create_directories(tmp_path.parent_path());
        std::ofstream os(tmp_path, std::ios::binary | std::ios::trunc);
        if (!os) throw std::runtime_error("prefill open failed: " + tmp_path.string());

        static const std::size_t kBlock = 1 << 20; // 1MB
        std::vector<unsigned char> buf(kBlock, 0xFF);

        std::uint64_t left = bytes;
        while (left > 0) {
            std::size_t n = (std::size_t)std::min<std::uint64_t>(left, kBlock);
            os.write(reinterpret_cast<const char*>(buf.data()), (std::streamsize)n);
            if (!os) throw std::runtime_error("prefill write failed: " + tmp_path.string());
            left -= n;
        }
    }

    void NodeGridPacker::ensure_preallocated(CitypeState& st) {
        const fs::path citype_dir = layout_.nodes_packed / st.citype;
        fs::create_directories(citype_dir);

        const fs::path attr_final = citype_dir / "attributes.bin";
        const fs::path meta_final = citype_dir / "metaAttributes.bin";

        if (citype_done_marker_exists(citype_dir) && fs::exists(attr_final) && fs::exists(meta_final)) {
            st.done = true;
            return;
        }

        const fs::path attr_tmp = citype_dir / "attributes.bin.tmp";
        const fs::path meta_tmp = citype_dir / "metaAttributes.bin.tmp";

        const std::uint64_t attr_bytes = st.N * (std::uint64_t)st.A * 4ull;
        const std::uint64_t meta_bytes = st.N * 6ull * 4ull;

        // fresh prealloc every run unless already in-progress temp exists
        if (!fs::exists(attr_tmp) || fs::file_size(attr_tmp) != attr_bytes) {
            prefill_file_with_ff(attr_tmp, attr_bytes);
        }
        if (!fs::exists(meta_tmp) || fs::file_size(meta_tmp) != meta_bytes) {
            prefill_file_with_ff(meta_tmp, meta_bytes);
        }

        st.attr.open(attr_tmp, std::ios::in | std::ios::out | std::ios::binary);
        st.meta.open(meta_tmp, std::ios::in | std::ios::out | std::ios::binary);
        if (!st.attr || !st.meta) throw std::runtime_error("failed to open tmp grid files for " + st.citype);
    }

    NodeGridPacker::CitypeState& NodeGridPacker::get_citype(const std::string& citype) {
        auto it = cache_.find(citype);
        if (it != cache_.end()) return it->second;

        CitypeState st;
        st.citype = citype;

        const fs::path citype_dir = layout_.nodes_packed / citype;
        const fs::path fmt_json   = citype_dir / "format.json";
        const fs::path attrs_json = citype_dir / "attributes.json";
        const fs::path uuids_bin  = citype_dir / "uuids.bin";

        // format.json
        {
            std::ifstream is(fmt_json, std::ios::binary);
            if (!is) throw std::runtime_error("open failed: " + fmt_json.string());
            json j; is >> j;
            st.A = (std::uint32_t)j.value("attributeCount", 0);
            if (st.A == 0 && fs::exists(attrs_json)) {
                // attributeCount can legitimately be 0, but you probably want consistency:
                // keep as 0 if attributes.json empty
            }
        }

        st.attr_index = load_attr_index(attrs_json);
        st.uuids      = load_uuids(uuids_bin);
        st.N          = st.uuids.size();

        cache_.emplace(citype, std::move(st));
        CitypeState& ref = cache_.at(citype);
        ensure_preallocated(ref);
        return ref;
    }

    void NodeGridPacker::ingest(const json& j) {
        if (!j.contains("type") || !j["type"].is_string()) return;
        const std::string citype = j["type"].get<std::string>();

        if (!j.contains("uuid") || !j["uuid"].is_string()) return;

        Uuid128 u;
        try {
            u = uuid_from_string(j["uuid"].get_ref<const std::string&>());
        } catch (...) {
            return;
        }

        CitypeState& st = get_citype(citype);
        if (st.done) return; // already packed

        // resolve local_index via binary search
        auto it = std::lower_bound(st.uuids.begin(), st.uuids.end(), u);
        if (it == st.uuids.end() || !(*it == u)) return;
        const std::uint32_t li = (std::uint32_t)std::distance(st.uuids.begin(), it);

        // build attribute row
        std::vector<std::int32_t> row(st.A, kMissingI32);

        if (st.A > 0 && j.contains("attributes") && j["attributes"].is_array()) {
            for (const auto& a : j["attributes"]) {
                if (!a.is_object()) continue;
                if (!a.contains("name") || !a["name"].is_string()) continue;
                if (!a.contains("value")) continue;

                auto itA = st.attr_index.find(a["name"].get<std::string>());
                if (itA == st.attr_index.end()) continue;

                const std::string canon = canonical_value(a["value"]);
                DictIndex di;
                if (!dict_.try_find(canon, di)) {
                    throw std::runtime_error(
                        "dict miss: citype=" + st.citype +
                        " attr=" + a["name"].get<std::string>() +
                        " canon=" + canon
                    );
                }

                row[itA->second] = (std::int32_t)di;
            }
        }

        // meta row (always 6)
        std::int32_t mrow[6];
        for (int k = 0; k < 6; ++k) mrow[k] = kMissingI32;

        if (j.contains("metaAttributes") && j["metaAttributes"].is_object()) {
            const auto& m = j["metaAttributes"];
            for (int k = 0; k < 6; ++k) {
                const char* key = kMetaKeys[k];
                if (!m.contains(key)) continue;
                const std::string canon = canonical_value(m[key]);
                DictIndex di;
                if (!dict_.try_find(canon, di)) {
                    throw std::runtime_error(
                        "dict miss: citype=" + st.citype +
                        " metaKey=" + std::string(key) +
                        " canon=" + canon
                    );
                }
                mrow[k] = (std::int32_t)di;
            }
        }

        // random write
        const std::uint64_t attr_off = (std::uint64_t)li * (std::uint64_t)st.A * 4ull;
        st.attr.seekp((std::streamoff)attr_off);
        for (std::uint32_t k = 0; k < st.A; ++k) write_i32_le(st.attr, row[k]);

        const std::uint64_t meta_off = (std::uint64_t)li * 6ull * 4ull;
        st.meta.seekp((std::streamoff)meta_off);
        for (int k = 0; k < 6; ++k) write_i32_le(st.meta, mrow[k]);
    }

    void NodeGridPacker::finalize() {
        for (auto& kv : cache_) {
            CitypeState& st = kv.second;
            const fs::path citype_dir = layout_.nodes_packed / st.citype;

            if (st.done) continue;

            st.attr.flush();
            st.meta.flush();
            st.attr.close();
            st.meta.close();

            atomic_rename(citype_dir / "attributes.bin.tmp", citype_dir / "attributes.bin");
            atomic_rename(citype_dir / "metaAttributes.bin.tmp", citype_dir / "metaAttributes.bin");

            std::cerr << "[pass2:nodes] wrote "
                    << st.citype
                    << " N=" << st.N
                    << " attrs=" << st.A
                    << "\n";

            mark_done(citype_dir, ".pass2.done", "pass=2\nkind=nodes\n");
            st.done = true;
        }
    }

}