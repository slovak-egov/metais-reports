#include "pass2_pack.h"
#include "traverse_raw.h"
#include "progress.h"
#include "dict_lookup.h"
#include "global_uuid_index.h"
#include "resolver_index.h"
#include "node_grid_packer.h"
#include "relation_grid_packer.h"
#include "step_marker.h"

#include <nlohmann/json.hpp>
#include <fstream>

namespace fs = std::filesystem;

namespace metais {

    static std::vector<std::string> load_citypes_vec_local(const fs::path& citypes_json) {
        std::ifstream is(citypes_json, std::ios::binary);
        if (!is) throw std::runtime_error("open failed: " + citypes_json.string());
        nlohmann::json j; is >> j;
        if (!j.is_array()) throw std::runtime_error("citypes.json must be array");
        std::vector<std::string> v;
        v.reserve(j.size());
        for (const auto& x : j) v.push_back(x.get<std::string>());
        return v;
    }

    static void write_rels_manifest_atomic(const fs::path& rels_root) {
        using json = nlohmann::json;

        // bySource[type] -> set(reltype)
        std::unordered_map<std::string, std::vector<std::string>> bySource;
        std::unordered_map<std::string, std::vector<std::string>> byTarget;

        for (const auto& ent : fs::directory_iterator(rels_root)) {
            if (!ent.is_directory()) continue;

            const fs::path rel_dir = ent.path();
            const std::string reltype = rel_dir.filename().string();

            fs::path ep_path = rel_dir / "endpoints.json";
            if (!fs::exists(ep_path)) continue;

            json ep;
            {
                std::ifstream is(ep_path, std::ios::binary);
                if (!is) throw std::runtime_error("open failed: " + ep_path.string());
                is >> ep;
            }

            if (ep.contains("sourceTypes") && ep["sourceTypes"].is_array()) {
                for (const auto& s : ep["sourceTypes"]) {
                    if (!s.is_string()) continue;
                    bySource[s.get<std::string>()].push_back(reltype);
                }
            }
            if (ep.contains("targetTypes") && ep["targetTypes"].is_array()) {
                for (const auto& t : ep["targetTypes"]) {
                    if (!t.is_string()) continue;
                    byTarget[t.get<std::string>()].push_back(reltype);
                }
            }
        }

        // sort+dedupe each list for determinism
        auto sort_dedupe = [](std::vector<std::string>& v) {
            std::sort(v.begin(), v.end());
            v.erase(std::unique(v.begin(), v.end()), v.end());
        };
        for (auto& kv : bySource) sort_dedupe(kv.second);
        for (auto& kv : byTarget) sort_dedupe(kv.second);

        // emit JSON
        json out;
        out["bySource"] = json::object();
        out["byTarget"] = json::object();

        // stable key order: dump() won’t guarantee object key order,
        // but most JSON libs keep insertion order — we can enforce it:
        {
            std::vector<std::string> keys;
            keys.reserve(bySource.size());
            for (auto& kv : bySource) keys.push_back(kv.first);
            std::sort(keys.begin(), keys.end());
            for (auto& k : keys) out["bySource"][k] = bySource[k];
        }
        {
            std::vector<std::string> keys;
            keys.reserve(byTarget.size());
            for (auto& kv : byTarget) keys.push_back(kv.first);
            std::sort(keys.begin(), keys.end());
            for (auto& k : keys) out["byTarget"][k] = byTarget[k];
        }

        // atomic write
        fs::path final = rels_root / "relations.json";
        fs::path tmp   = final; tmp += ".tmp";
        {
            std::ofstream os(tmp, std::ios::binary);
            if (!os) throw std::runtime_error("open failed: " + tmp.string());
            const std::string s = out.dump(2) + "\n";
            os.write(s.data(), (std::streamsize)s.size());
            if (!os) throw std::runtime_error("write failed: " + tmp.string());
        }
        metais::atomic_rename(tmp, final);
    }

    void pass2_pack_nodes_and_relations(const DirectoryLayout& layout, bool skip_bad_json) {
        std::cerr << "[pass2] starting\n";

        if (!is_done(layout.packed_root, ".pass1_5.done")) {
            throw std::runtime_error("Pass 2 requires Pass 1.5 outputs");
        }

        if (is_done(layout.packed_root, ".pass2.done")) {
            std::cerr << "[pass2] already done; skipping\n";
            return;
        }
        
        std::cerr << "[pass2] loading dict...\n";
        DictLookup dict;
        dict.load(layout.dict_dir);
        std::cerr << "[pass2] dict loaded\n";

        std::cerr << "[pass2] loading global uuids...\n";
        GlobalUuidIndex gu;
        gu.load(layout.uuids_dir / "uuids.bin");
        std::cerr << "[pass2] uuids loaded, N=" << gu.size() << "\n";

        std::cerr << "[pass2] loading resolver...\n";
        GlobalResolverIndex gr;
        gr.load(layout.uuids_dir / "resolver.bin", gu.size());
        std::cerr << "[pass2] resolver loaded\n";

        std::cerr << "[pass2] loading citypes...\n";
        auto citypes = load_citypes_vec_local(layout.uuids_dir / "citypes.json");
        std::cerr << "[pass2] citypes loaded, n=" << citypes.size() << "\n";

        std::cerr << "[pass2] constructing node_packer...\n";
        NodeGridPacker node_packer(layout, dict);
        std::cerr << "[pass2] node_packer ok\n";

        std::cerr << "[pass2] constructing rel_packer...\n";
        RelationGridPacker rel_packer(layout, dict, gu, gr, citypes);
        std::cerr << "[pass2] rel_packer ok\n";

        // ---- nodes ----
        if (!is_done(layout.packed_root, ".pass2.nodes.done")) {
            std::cerr << "[pass2] packing nodes\n";
            {
                const auto pages_dir = layout.raw_nodes_dir / "pages";
                std::size_t seen = 0;
                const auto shards = list_shards_by_meta(pages_dir, "nodes");
                ProgressBar shard_bar("pass2 nodes shards", shards.size());

                std::size_t last_shard = (std::size_t)-1;

                for (auto&& rec : ndjson_json_range(pages_dir, "nodes", skip_bad_json)) {
                    if (rec.shard_index != last_shard) {
                        last_shard = rec.shard_index;
                        shard_bar.update(last_shard + 1);
                    }
                    node_packer.ingest(rec.obj);
                    ++seen;
                }
                shard_bar.finish();
                node_packer.finalize();
                std::cerr << "[pass2] nodes done, records=" << seen << "\n";

                mark_done(layout.packed_root, ".pass2.nodes.done", "pass=2\nkind=nodes\n");
            }
        } else {
            std::cerr << "[pass2] nodes already done, skipping\n";
        }

        // ---- relations ----
        if (!is_done(layout.packed_root, ".pass2.rels.done")) {
            std::cerr << "[pass2] packing relations\n";
            {
                const auto pages_dir = layout.raw_rels_dir / "pages";
                std::size_t seen = 0;
                const auto shards = list_shards_by_meta(pages_dir, "rels");
                ProgressBar shard_bar("pass2 rels shards", shards.size());

                std::size_t last_shard = (std::size_t)-1;

                for (auto&& rec : ndjson_json_range(pages_dir, "rels", skip_bad_json)) {
                    if (rec.shard_index != last_shard) {
                        last_shard = rec.shard_index;
                        shard_bar.update(last_shard + 1);
                    }
                    rel_packer.ingest(rec.obj);
                    ++seen;
                }
                shard_bar.finish();
                rel_packer.finalize();
                std::cerr << "[pass2] rels done, records=" << seen << "\n";

                mark_done(layout.packed_root, ".pass2.rels.done", "pass=2\nkind=rels\n");

                write_rels_manifest_atomic(layout.rels_packed);
            }
        } else {
            std::cerr << "[pass2] rels already done, skipping\n";
        }
        
        const bool nodes_done = is_done(layout.packed_root, ".pass2.nodes.done");
        const bool rels_done  = is_done(layout.packed_root, ".pass2.rels.done");

        if (nodes_done && rels_done) {
            mark_done(layout.packed_root, ".pass2.done", "pass=2\n");
            std::cerr << "[pass2] done\n";
        } else {
            std::cerr << "[pass2] incomplete: nodes_done=" << nodes_done
                    << " rels_done=" << rels_done << "\n";
        }
    }

}