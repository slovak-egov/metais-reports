#include "prepass.h"
#include "traverse_raw.h"
#include "progress.h"
#include "canonical_value.h"

namespace metais {

    void prepass(std::string tag, const DirectoryLayout& layout, PrepassResult& out, bool skip_bad_json) {
        auto t0 = std::chrono::steady_clock::now();
        std::uint64_t last_report = 0;

        bool parsingNodes = false;

        PrepassStats* stats = nullptr;
        AttributeCatalog* cat = nullptr;
        AttributeCatalog* metaCat = nullptr;
        std::vector<Uuid128>* uuids = nullptr;

        std::filesystem::path pages_dir;

        if ((tag == "node") || (tag == "nodes") || (tag == "entity") || (tag == "entities")) {
            pages_dir    = layout.raw_nodes_dir / "pages";
            tag          = "nodes";
            parsingNodes = true;

            stats   = &out.nodes;
            cat     = &out.attrs_ent;
            metaCat = &out.metaAttrs_ent;
            uuids   = &out.uuids_ent;
        } else {
            pages_dir = layout.raw_rels_dir / "pages";
            tag       = "rels";

            stats   = &out.rels;
            cat     = &out.attrs_rel;
            metaCat = &out.metaAttrs_rel;
            // uuids = &out.uuids_rel; // if you ever collect rel uuids
        }

        auto& dct = out.dict;

        const auto shards = list_shards_by_meta(pages_dir, tag);
        ProgressBar shard_bar(tag + " shards", shards.size());

        std::size_t last_shard = (std::size_t)-1;

        for (auto&& rec : ndjson_json_range(pages_dir, tag, skip_bad_json)) {
            ++stats->total_records;

            if (rec.shard_index != last_shard) {
                last_shard = rec.shard_index;
                shard_bar.update(rec.shard_index + 1);
            }

            const auto& j = rec.obj;

            if (!j.contains("type") || !j["type"].is_string()) {
                ++stats->missing_type;
                continue;
            }
            const std::string citype = j["type"].get<std::string>();
            cat->note_object(citype);

            // ---- UUID (nodes only) ----
            if (parsingNodes) {
                if (!j.contains("uuid") || !j["uuid"].is_string()) {
                    ++stats->missing_uuid;
                } else {
                    const auto& s = j["uuid"].get_ref<const std::string&>();
                    try {
                        Uuid128 u = uuid_from_string(std::string_view{s});

                        // existing flat list (optional but fine)
                        uuids->push_back(u);

                        // per-citype ownership
                        out.uuids_by_citype[citype].push_back(u);

                    } catch (...) {
                        ++stats->bad_uuid;
                    }
                }
            }

            // ---- Attributes ----
            if (j.contains("attributes") && j["attributes"].is_array()) {
                for (const auto& a : j["attributes"]) {
                    if (!a.is_object()) {
                        ++stats->bad_attributes_type;
                        continue;
                    }
                    if (!a.contains("name") || !a["name"].is_string()) {
                        continue;
                    }
                    const std::string name = a["name"].get<std::string>();
                    cat->note_attr(citype, name);

                    if (!a.contains("value")) continue;
                    dct.note(canonical_value(a["value"]));
                }
            } else {
                ++stats->missing_attributes;
            }

            // ---- MetaAttributes (optional but I’d start capturing now) ----
            if (j.contains("metaAttributes") && j["metaAttributes"].is_object()) {
                const auto& m = j["metaAttributes"];
                for (auto it = m.begin(); it != m.end(); ++it) {
                    metaCat->note_attr(citype, it.key());
                    dct.note(canonical_value(it.value()));
                }
            }
        }

        shard_bar.finish();
    }

}