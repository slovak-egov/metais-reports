#include "relation_grid_packer.h"
#include "step_marker.h"


#include <algorithm>
#include <optional>

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace metais {

    RelationGridPacker::RelationGridPacker(
        const DirectoryLayout& layout,
        const DictLookup& dict,
        const GlobalUuidIndex& gu,
        const GlobalResolverIndex& resolver,
        const std::vector<std::string>& citypes
    )
        : layout_(layout)
        , dict_(dict)
        , gu_(gu)
        , resolver_(resolver)
        , citypes_(citypes)
    {}

    bool RelationGridPacker::rel_done_marker_exists(const fs::path& rel_dir) {
        return fs::exists(rel_dir / ".pass2.done");
    }

    std::unordered_map<std::string, std::uint32_t>
    RelationGridPacker::load_attr_index(const fs::path& attributes_json) {
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

    RelationGridPacker::RelState& RelationGridPacker::get_reltype(const std::string& reltype) {
        auto it = cache_.find(reltype);
        if (it != cache_.end()) return it->second;

        RelState st;
        st.reltype = reltype;

        const fs::path rel_dir   = layout_.rels_packed / reltype;
        const fs::path fmt_json  = rel_dir / "format.json";
        const fs::path attrs_json= rel_dir / "attributes.json";

        fs::create_directories(rel_dir);

        if (rel_done_marker_exists(rel_dir) &&
            fs::exists(rel_dir / "tmp.edges.bin") &&
            fs::exists(rel_dir / "attributes.bin") &&
            fs::exists(rel_dir / "metaAttributes.bin")) {
            st.done = true;
            cache_.emplace(reltype, std::move(st));
            return cache_.at(reltype);
        }

        // format.json
        {
            std::ifstream is(fmt_json, std::ios::binary);
            if (!is) throw std::runtime_error("open failed: " + fmt_json.string());
            json j; is >> j;
            st.A = (std::uint32_t)j.value("attributeCount", 0);
        }

        st.attr_index = load_attr_index(attrs_json);

        // open append-only temp outputs
        st.edges.open((rel_dir / "tmp.edges.bin"), std::ios::binary | std::ios::trunc);
        st.attr .open((rel_dir / "tmp.attributes.bin"), std::ios::binary | std::ios::trunc);
        st.meta .open((rel_dir / "tmp.metaAttributes.bin"), std::ios::binary | std::ios::trunc);
        if (!st.edges || !st.attr || !st.meta) throw std::runtime_error("failed opening rel tmp outputs: " + reltype);

        cache_.emplace(reltype, std::move(st));
        return cache_.at(reltype);
    }

    void RelationGridPacker::ingest(const json& j) {
        if (!j.contains("type") || !j["type"].is_string()) return;
        const std::string reltype = j["type"].get<std::string>();

        RelState& st = get_reltype(reltype);
        if (st.done) return;

        // obtain source and target uuid
        std::optional<Uuid128> su, tu;

        if (j.contains("startUuid") && j["startUuid"].is_string()) {
            try { su = uuid_from_string(j["startUuid"].get_ref<const std::string&>()); } catch(...) {}
        }
        if (j.contains("endUuid") && j["endUuid"].is_string()) {
            try { tu = uuid_from_string(j["endUuid"].get_ref<const std::string&>()); } catch(...) {}
        }
        if (!su || !tu) return;

        GlobalId src_gid, tgt_gid;
        if (!gu_.try_resolve(*su, src_gid)) return;
        if (!gu_.try_resolve(*tu, tgt_gid)) return;

        // append edge pair
        write_u32_le(st.edges, src_gid);
        write_u32_le(st.edges, tgt_gid);

        const CitypeIndex sci = resolver_.citype_index_of(src_gid);
        const CitypeIndex tci = resolver_.citype_index_of(tgt_gid);

        if ((std::size_t)sci < citypes_.size()) st.source_types.insert(citypes_[sci]);
        if ((std::size_t)tci < citypes_.size()) st.target_types.insert(citypes_[tci]);

        // build attribute row
        for (std::uint32_t k = 0; k < st.A; ++k) write_i32_le(st.attr, kMissingI32);

        if (st.A > 0 && j.contains("attributes") && j["attributes"].is_array()) {
            // seek back and overwrite where needed: easiest is build a small row buffer then write once.
            // For simplicity + speed, do the buffer way:
            std::vector<std::int32_t> row(st.A, kMissingI32);
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
                        "dict miss: reltype=" + st.reltype +
                        " attr=" + a["name"].get<std::string>() +
                        " canon=" + canon
                    );
                }
                row[itA->second] = (std::int32_t)di;
            }

            // rewrite: easiest is to back up st.A*4 bytes and rewrite the row
            st.attr.seekp(-std::streamoff(st.A * 4u), std::ios::cur);
            for (std::uint32_t k = 0; k < st.A; ++k) write_i32_le(st.attr, row[k]);
        }

        // meta row (6)
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
                        "dict miss: reltype=" + st.reltype +
                        " metaKey=" + std::string(key) +
                        " canon=" + canon
                    );
                }
                mrow[k] = (std::int32_t)di;
            }
        }
        for (int k = 0; k < 6; ++k) write_i32_le(st.meta, mrow[k]);

        st.count++;
    }

    void RelationGridPacker::write_endpoints_json_atomic(const fs::path& rel_dir, const RelState& st) {
        json ep;
        ep["sourceTypes"] = json::array();
        ep["targetTypes"] = json::array();

        for (const auto& s : st.source_types) ep["sourceTypes"].push_back(s);
        for (const auto& s : st.target_types) ep["targetTypes"].push_back(s);

        std::sort(ep["sourceTypes"].begin(), ep["sourceTypes"].end());
        std::sort(ep["targetTypes"].begin(), ep["targetTypes"].end());

        fs::path p = rel_dir / "endpoints.json";
        fs::path tmp = p; tmp += ".tmp";
        std::ofstream os(tmp, std::ios::binary);
        auto dump = ep.dump();
        os.write(dump.data(), (std::streamsize)dump.size());
        atomic_rename(tmp, p);
    }

    // for warning print of multiple src/tgt types
    static std::string join_types(const std::unordered_set<std::string>& s) {
        std::vector<std::string> v(s.begin(), s.end());
        std::sort(v.begin(), v.end());
        std::string out;
        for (std::size_t i = 0; i < v.size(); ++i) {
            if (i) out += ", ";
            out += v[i];
        }
        return out;
    }

    void RelationGridPacker::finalize() {
        for (auto& kv : cache_) {
            RelState& st = kv.second;
            const fs::path rel_dir = layout_.rels_packed / st.reltype;
            if (st.done) continue;

            st.edges.flush(); st.attr.flush(); st.meta.flush();
            st.edges.close(); st.attr.close(); st.meta.close();

            atomic_rename(rel_dir / "tmp.attributes.bin", rel_dir / "attributes.bin");
            atomic_rename(rel_dir / "tmp.metaAttributes.bin", rel_dir / "metaAttributes.bin");

            // count.json
            {
                json j;
                j["relationCount"] = (std::uint64_t)st.count;
                fs::path p = rel_dir / "pass2.count.json";
                fs::path tmp = p; tmp += ".tmp";
                std::ofstream os(tmp, std::ios::binary);
                const std::string s = j.dump();
                os.write(s.data(), (std::streamsize)s.size());
                atomic_rename(tmp, p);
            }

            // endpoints.json
            write_endpoints_json_atomic(rel_dir, st);

            mark_done(rel_dir, ".pass2.done", "pass=2\nkind=rels\n");
            st.done = true;

            std::cerr << "[pass2:rels] wrote "
                    << st.reltype
                    << " M=" << st.count
                    << " srcTypes=" << st.source_types.size()
                    << " tgtTypes=" << st.target_types.size()
                    << "\n";
            
            // print warning if multiple src/tgt types
            if (st.source_types.size() != 1) {
                std::cerr << "[pass2:rels][WARN] " << st.reltype
                        << " multiple source types (" << st.source_types.size() << "): "
                        << join_types(st.source_types) << "\n\n";
            }
            if (st.target_types.size() != 1) {
                std::cerr << "[pass2:rels][WARN] " << st.reltype
                        << " multiple target types (" << st.target_types.size() << "): "
                        << join_types(st.target_types) << "\n\n";
            }

        }

    }

}