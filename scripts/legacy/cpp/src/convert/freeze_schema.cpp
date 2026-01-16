#include "freeze_schema.h"
#include "binary_formats.h"   // for write_u64_le, write_uuid_raw16, etc.
#include "json_utils.h"       // if you have load/save helpers, otherwise use ifstream/ofstream
#include "canonical_value.h"
#include "progress.h"
#include "step_marker.h"

#include <nlohmann/json.hpp>
#include <filesystem>
#include <fstream>
#include <unordered_map>
#include <vector>
#include <algorithm>
#include <stdexcept>
#include <ctime>

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace metais {

    static std::string now_utc_epoch() {
        return std::to_string((long long)std::time(nullptr));
    }

    static std::uint64_t count_total_nodes(const metais::PrepassResult& pre) {
        std::uint64_t total = 0;
        for (const auto& kv : pre.uuids_by_citype) total += kv.second.size();
        return total;
    }

    // -------------------------
    // Atomic write helpers
    // -------------------------
    static void write_atomic_json(const fs::path& path, const json& j) {
        fs::create_directories(path.parent_path());
        fs::path tmp = path;
        tmp += ".tmp";

        {
            std::ofstream os(tmp, std::ios::binary);
            if (!os) throw std::runtime_error("open failed: " + tmp.string());
            const std::string s = j.dump(); // compact
            os.write(s.data(), (std::streamsize)s.size());
            if (!os) throw std::runtime_error("write failed: " + tmp.string());
        }
        atomic_rename(tmp, path);
    }

    static json read_json_file(const fs::path& p) {
        std::ifstream is(p, std::ios::binary);
        if (!is) throw std::runtime_error("Failed to open JSON: " + p.string());
        json j;
        is >> j;
        return j;
    }

    // -------------------------
    // Attribute metadata index
    // -------------------------
    struct AttrMeta {
        std::string name;
        std::string description;
        std::string hasEnum; // enum code or "" if none
        bool has = false;
    };

    static void index_attr_array(std::unordered_map<std::string, AttrMeta>& out, const json& arr) {
        if (!arr.is_array()) return;
        for (const auto& a : arr) {
            if (!a.is_object()) continue;
            if (!a.contains("technicalName") || !a["technicalName"].is_string()) continue;

            const std::string tech = a["technicalName"].get<std::string>();

            AttrMeta m;
            m.has = true;

            if (a.contains("name") && a["name"].is_string()) m.name = a["name"].get<std::string>();
            if (a.contains("description") && a["description"].is_string()) m.description = a["description"].get<std::string>();

            // prefer constraints[*].enumCode, fallback attributeTypeEnum (less useful)
            if (a.contains("constraints") && a["constraints"].is_array()) {
                for (const auto& c : a["constraints"]) {
                    if (!c.is_object()) continue;
                    if (c.contains("enumCode") && c["enumCode"].is_string()) {
                        m.hasEnum = c["enumCode"].get<std::string>();
                        break;
                    }
                }
            }
            out[tech] = std::move(m);
        }
    }

    static std::unordered_map<std::string, AttrMeta> load_type_attr_meta(const fs::path& meta_file) {
        std::unordered_map<std::string, AttrMeta> idx;

        if (!fs::exists(meta_file)) return idx; // allowed: metadata missing

        const json j = read_json_file(meta_file);

        // top-level attributes (if present)
        if (j.contains("attributes")) {
            index_attr_array(idx, j["attributes"]);
        }

        // attributeProfiles[*].attributes
        if (j.contains("attributeProfiles") && j["attributeProfiles"].is_array()) {
            for (const auto& prof : j["attributeProfiles"]) {
                if (!prof.is_object()) continue;
                if (prof.contains("attributes")) index_attr_array(idx, prof["attributes"]);
            }
        }

        return idx;
    }

    // -------------------------
    // Write per-type attributes.json + format.json
    // -------------------------
    static void write_type_schema_files(
        const fs::path& out_dir,
        const fs::path& meta_file,
        const std::unordered_set<std::string>& observed_attrs
    ) {
        fs::create_directories(out_dir);

        // 1) sorted observed tech names
        std::vector<std::string> names;
        names.reserve(observed_attrs.size());
        for (const auto& s : observed_attrs) names.push_back(s);
        std::sort(names.begin(), names.end());

        // 2) optional enrichment from metadata file
        const auto meta_idx = load_type_attr_meta(meta_file);

        json attrs = json::array();

        for (const auto& tech : names) {
            json item;
            item["technicalName"] = tech;

            auto it = meta_idx.find(tech);
            if (it != meta_idx.end() && it->second.has) {
                item["name"] = it->second.name.empty() ? json(nullptr) : json(it->second.name);
                item["description"] = it->second.description.empty() ? json(nullptr) : json(it->second.description);
                item["hasEnum"] = it->second.hasEnum.empty() ? json(nullptr) : json(it->second.hasEnum);
            } else {
                item["name"] = nullptr;
                item["description"] = nullptr;
                item["hasEnum"] = nullptr;
            }

            attrs.push_back(std::move(item));
        }

        // 3) format.json (grid-first implementation)
        json fmt;
        fmt["attributeLayout"] = "grid";
        fmt["attributeCount"] = (std::uint64_t)names.size();
        fmt["metaAttributeCount"] = 6;

        write_atomic_json(out_dir / "attributes.json", attrs);
        write_atomic_json(out_dir / "format.json", fmt);
    }

    static void write_dict_files(const DirectoryLayout& layout, ValueDictionary& dict) {
        phase("[freeze] Finalizing dictionary (sort + index)...");
        dict.finalize_sorted();

        fs::create_directories(layout.dict_dir);

        const fs::path dict_bin = layout.dict_dir / "dict.bin";
        const fs::path off_bin  = layout.dict_dir / "dict.offsets.bin";
        const fs::path meta_js  = layout.dict_dir / "meta.json";

        std::string blob;
        blob.reserve(dict.values.size() * 16);

        std::vector<std::uint64_t> offs;
        offs.reserve(dict.values.size() + 1);

        std::uint64_t cur = 0;
        offs.push_back(cur);

        phase("[freeze] Building dict.bin + offsets in memory...");
        ProgressBar pb("dict concat", dict.values.size());
        std::size_t i = 0;

        const std::size_t step = std::max<std::size_t>(1, dict.values.size() / 200);
        for (const auto& s : dict.values) {
            // IMPORTANT: s must already be JSON literal text
            blob.append(s);
            cur += (std::uint64_t)s.size();
            if ((++i % step) == 0) pb.update(i);
            offs.push_back(cur);
        }
        pb.update(dict.values.size());
        pb.finish();

        json meta;
        meta["valueCount"] = (std::uint64_t)dict.values.size();

        phase("[freeze] Writing dict.bin...");
        write_atomic_string(dict_bin, blob);
        phase("[freeze] Writing dict.offsets.bin...");
        write_atomic_u64le_file(off_bin, offs);
        phase("[freeze] Writing dict/meta.json...");
        write_atomic_json(meta_js, meta);

        std::cerr << "[dict] wrote valueCount=" << dict.values.size()
                << ", bytes=" << blob.size()
                << ", offsets=" << offs.size()
                << "\n";
    }

    static std::vector<std::string> observed_citypes_from_pre(const PrepassResult& pre) {
        std::vector<std::string> v;
        v.reserve(pre.uuids_by_citype.size());
        for (const auto& kv : pre.uuids_by_citype) v.push_back(kv.first);
        std::sort(v.begin(), v.end());
        return v;
    }

    static std::vector<std::string> load_citypes_list_keep_order(const fs::path& p) {
        if (!fs::exists(p)) return {};
        json j = read_json_file(p);
        if (!j.is_array()) throw std::runtime_error("citypes_list.json must be a JSON array");

        std::unordered_set<std::string> seen;
        std::vector<std::string> out;
        for (const auto& x : j) {
            if (!x.is_string()) continue;
            std::string s = x.get<std::string>();
            if (seen.insert(s).second) out.push_back(std::move(s)); // dedupe, preserve order
        }
        return out;
    }

    static std::vector<std::string> write_citypes(const DirectoryLayout& layout, const PrepassResult& pre) {
        // observed fallback / supplement
        const auto observed = observed_citypes_from_pre(pre);

        // source-of-truth list if present
        auto final_list = load_citypes_list_keep_order(layout.citypes_list_json);

        if (!final_list.empty()) {
            std::unordered_set<std::string> already(final_list.begin(), final_list.end());
            for (const auto& c : observed) {
                if (!already.count(c)) final_list.push_back(c); // observed extras appended
            }
        } else {
            final_list = observed; // fallback: deterministic sort
        }

        std::cerr << "[citypes] metadata list: " << (fs::exists(layout.citypes_list_json) ? "yes" : "no")
                << ", observed=" << observed.size()
                << ", final=" << final_list.size()
                << "\n";

        write_atomic_json(layout.uuids_dir / "citypes.json", json(final_list));
        std::cerr << "[citypes] wrote " << (layout.uuids_dir / "citypes.json") << "\n";

        return final_list;
    }

    static std::unordered_map<std::string, CitypeIndex>
    build_citype_index_map(const fs::path& citypes_json_path) {
        json j = read_json_file(citypes_json_path);
        if (!j.is_array()) throw std::runtime_error("citypes.json must be array");

        std::unordered_map<std::string, CitypeIndex> m;
        CitypeIndex idx = 0;
        for (const auto& x : j) {
            if (!x.is_string()) continue;
            m.emplace(x.get<std::string>(), idx++);
        }
        return m;
    }

    static void write_citype_uuids(
        const DirectoryLayout& layout,
        PrepassResult& pre
    ) {
        phase("[freeze] Writing per-citype uuids.bin...");
        ProgressBar pb("citype uuids", pre.uuids_by_citype.size());

        std::size_t k = 0;
        for (auto& kv : pre.uuids_by_citype) {
            pb.update(++k);

            const std::string& citype = kv.first;
            auto& v = kv.second;

            auto before = v.size();
            std::sort(v.begin(), v.end());
            v.erase(std::unique(v.begin(), v.end()), v.end());
            auto after = v.size();
            if (after != before) {
                std::cerr << "[uuids] " << citype << ": removed " << (before - after) << " duplicate uuids\n";
            }

            const fs::path out = layout.nodes_packed / citype / "uuids.bin";
            write_atomic_uuid16_file(out, v);
        }
        pb.finish();

        std::cerr << "[uuids] wrote uuids.bin for " << pre.uuids_by_citype.size() << " citypes\n";
    }

    struct GlobalUuidRec {
        Uuid128 uuid;
        CitypeIndex citype_index;
        LocalIndex  local_index; 
    };

    static void write_global_uuid_resolver(const DirectoryLayout& layout, const PrepassResult& pre) {
        phase("[freeze] Writing global UUID resolver (uuids.bin + resolver.bin)...");

        // Use the function you currently have but don't use (this removes your warning too)
        const auto citype_index_of = build_citype_index_map(layout.uuids_dir / "citypes.json");

        // Count total nodes (UUID-bearing nodes)
        std::size_t total = 0;
        for (const auto& kv : pre.uuids_by_citype) total += kv.second.size();

        std::vector<GlobalUuidRec> recs;
        recs.reserve(total);

        // Build records from the already-sorted per-citype UUID vectors.
        // (We sorted/deduped them in write_citype_uuids; we can rely on that,
        //  or you can re-sort defensively if you prefer.)
        {
            ProgressBar pb("build uuid tuples", total);
            std::size_t done = 0;

            for (const auto& kv : pre.uuids_by_citype) {
                const std::string& citype = kv.first;
                const auto it = citype_index_of.find(citype);
                if (it == citype_index_of.end()) {
                    throw std::runtime_error("Citype '" + citype + "' missing from citypes.json");
                }
                const CitypeIndex ci = it->second;

                const auto& uuids = kv.second;
                for (std::uint32_t li = 0; li < (std::uint32_t)uuids.size(); ++li) {
                    recs.push_back(GlobalUuidRec{uuids[li], ci, li});
                    if ((++done % 200000) == 0) pb.update(done);
                }
            }
            pb.update(total);
            pb.finish();
        }

        phase("[freeze] Sorting global UUIDs...");
        {
            ProgressBar pb("sort global uuids", recs.size());
            // sort is “all at once”; progress bar isn’t super meaningful here, but keeping phase prints is useful
            std::sort(recs.begin(), recs.end(), [](const GlobalUuidRec& a, const GlobalUuidRec& b) {
                return a.uuid < b.uuid;
            });
            pb.update(recs.size());
            pb.finish();
        }

        const fs::path uuids_out    = layout.uuids_dir / "uuids.bin";
        const fs::path resolver_out = layout.uuids_dir / "resolver.bin";

        phase("[freeze] Writing uuids/uuids.bin...");
        {
            fs::create_directories(layout.uuids_dir);
            fs::path tmp = uuids_out; tmp += ".tmp";
            std::ofstream os(tmp, std::ios::binary);
            if (!os) throw std::runtime_error("open failed: " + tmp.string());
            for (const auto& r : recs) write_uuid_raw16(os, r.uuid);
            if (!os) throw std::runtime_error("write failed: " + tmp.string());
            atomic_rename(tmp, uuids_out);
        }

        phase("[freeze] Writing uuids/resolver.bin...");
        {
            fs::path tmp = resolver_out; tmp += ".tmp";
            std::ofstream os(tmp, std::ios::binary);
            if (!os) throw std::runtime_error("open failed: " + tmp.string());

            for (const auto& r : recs) {
                write_u16_le(os, (std::uint16_t)r.citype_index);
                write_u32_le(os, (std::uint32_t)r.local_index);
            }
            if (!os) throw std::runtime_error("write failed: " + tmp.string());
            atomic_rename(tmp, resolver_out);
        }

        std::cerr << "[uuids] global resolver: N=" << recs.size()
                << " (uuids.bin=" << (recs.size() * 16)
                << " bytes, resolver.bin=" << (recs.size() * 6)
                << " bytes)\n";
    }

    static void write_citype_global_ids(const DirectoryLayout& layout, const PrepassResult& pre) {
        phase("[freeze] Writing per-citype global_ids.bin...");

        // Read citypes.json just to map index -> citype string
        json j = read_json_file(layout.uuids_dir / "citypes.json");
        if (!j.is_array()) throw std::runtime_error("citypes.json must be array");

        std::vector<std::string> idx_to_citype;
        idx_to_citype.reserve(j.size());
        for (const auto& x : j) idx_to_citype.push_back(x.get<std::string>());

        // Prepare per-citype arrays sized to local counts
        std::vector<std::vector<GlobalId>> global_ids(idx_to_citype.size());
        for (std::size_t ci = 0; ci < idx_to_citype.size(); ++ci) {
            const auto it = pre.uuids_by_citype.find(idx_to_citype[ci]);
            if (it == pre.uuids_by_citype.end()) continue; // citype exists but had no UUIDs in this dump
            global_ids[ci].assign(it->second.size(), GlobalId{0});
        }

        // Stream resolver.bin and fill
        const fs::path resolver_path = layout.uuids_dir / "resolver.bin";
        std::ifstream is(resolver_path, std::ios::binary);
        if (!is) throw std::runtime_error("open failed: " + resolver_path.string());

        ProgressBar pb("global_ids fill", fs::file_size(layout.uuids_dir / "uuids.bin") / 16);
        GlobalId gid = 0;

        while (true) {
            if (!is.good()) break;
            if (is.peek() == EOF) break;

            const CitypeIndex ci = read_u16_le(is);
            const LocalIndex  li = read_u32_le(is);
            if (!is) break;

            if (ci >= global_ids.size()) throw std::runtime_error("resolver.bin: citype_index out of range");
            if (li >= global_ids[ci].size()) throw std::runtime_error("resolver.bin: local_index out of range");

            global_ids[ci][li] = gid;

            ++gid;
            if ((gid % 200000) == 0) pb.update(gid);
        }

        pb.update(gid);
        pb.finish();

        // Write each citype's global_ids.bin
        ProgressBar pbw("write global_ids.bin", idx_to_citype.size());
        for (std::size_t ci = 0; ci < idx_to_citype.size(); ++ci) {
            pbw.update(ci + 1);
            if (global_ids[ci].empty()) continue;

            const fs::path out = layout.nodes_packed / idx_to_citype[ci] / "global_ids.bin";
            write_atomic_u32le_file(out, global_ids[ci]);
        }
        pbw.finish();

        std::cerr << "[uuids] wrote global_ids.bin for " << idx_to_citype.size() << " citypes\n";
    }

    static void write_nodes_manifest(const DirectoryLayout& layout,
                                    const std::vector<std::string>& citypes_final) {
        json j;
        j["citypes"] = citypes_final;
        j["count"] = (std::uint64_t)citypes_final.size();
        j["schemaEpochUtc"] = now_utc_epoch();
        j["formatVersion"] = 1;

        write_atomic_json(layout.nodes_packed / "citypes_manifest.json", j);
    }

    static void write_reltypes_manifest(const DirectoryLayout& layout,
                                        const std::vector<std::string>& reltypes_final) {
        json j;
        j["reltypes"] = reltypes_final;
        j["count"] = (std::uint64_t)reltypes_final.size();
        j["schemaEpochUtc"] = now_utc_epoch();
        j["formatVersion"] = 1;

        write_atomic_json(layout.rels_packed / "reltypes_manifest.json", j);
    }

    // -------------------------
    // Pass 1.5 entry
    // -------------------------
    void freeze_schema_and_build_resolvers(const DirectoryLayout& layout, PrepassResult& pre) {

        clear_done(layout.packed_root, ".pass1_5.done");

        static const std::unordered_set<std::string> empty_attrs;

        // A) Per-citype schema files (nodes)
        phase("[freeze] Writing citype schemas...");

        // IMPORTANT: use uuids_by_citype, not seen_attrs_by_type
        ProgressBar pb_nodes("freeze node schemas", pre.uuids_by_citype.size());
        std::size_t i = 0;

        for (const auto& kv : pre.uuids_by_citype) {
            pb_nodes.update(++i);

            const std::string& citype = kv.first;

            auto it = pre.attrs_ent.seen_attrs_by_type.find(citype);
            const auto& observed = (it != pre.attrs_ent.seen_attrs_by_type.end())
                ? it->second
                : empty_attrs;

            const fs::path out_dir = layout.nodes_packed / citype;
            const fs::path meta_file = layout.nodes_meta_dir / (citype + ".json");

            write_type_schema_files(out_dir, meta_file, observed);
        }
        pb_nodes.finish();

        // B) Per-reltype schema files (relations)
        phase("[freeze] Writing reltype schemas...");

        // Drive off reltypes that actually exist in the dump (even if they have 0 attrs)
        ProgressBar pb_rels("freeze relation schemas", pre.attrs_rel.object_count_by_type.size());
        std::size_t j = 0;

        // Deterministic order (unordered_map iteration order is not deterministic)
        std::vector<std::string> reltypes;
        reltypes.reserve(pre.attrs_rel.object_count_by_type.size());
        for (const auto& kv : pre.attrs_rel.object_count_by_type) {
            reltypes.push_back(kv.first);
        }
        std::sort(reltypes.begin(), reltypes.end());

        for (const auto& reltype : reltypes) {
            pb_rels.update(++j);

            auto it = pre.attrs_rel.seen_attrs_by_type.find(reltype);
            const auto& observed = (it != pre.attrs_rel.seen_attrs_by_type.end())
                ? it->second
                : empty_attrs;

            const fs::path out_dir   = layout.rels_packed / reltype;
            const fs::path meta_file = layout.rels_meta_dir / (reltype + ".json");

            write_type_schema_files(out_dir, meta_file, observed);
        }
        pb_rels.finish();

        write_dict_files(layout, pre.dict);

        const auto citypes_final = write_citypes(layout, pre);

        write_citype_uuids(layout, pre);
        write_global_uuid_resolver(layout, pre);
        write_citype_global_ids(layout, pre);

        // nodes.json and relations.json listing all final citypes and reltypes (that exist)
        write_nodes_manifest(layout, citypes_final);
        write_reltypes_manifest(layout, reltypes);

        const std::uint64_t n_citypes = (std::uint64_t)pre.uuids_by_citype.size();
        const std::uint64_t n_nodes   = count_total_nodes(pre);
        const std::uint64_t dict_vals = (std::uint64_t)pre.dict.values.size();

        std::ostringstream ss;
        ss << "pass=1.5\n";
        ss << "date=" << layout.dump_date << "\n";
        ss << "utc_epoch=" << now_utc_epoch() << "\n";
        ss << "citypes=" << n_citypes << "\n";
        ss << "nodes=" << n_nodes << "\n";
        ss << "dict_values=" << dict_vals << "\n";

        mark_done(layout.packed_root, ".pass1_5.done", ss.str());

    }

}