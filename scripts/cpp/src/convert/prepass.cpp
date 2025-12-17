#include "prepass.h"
#include "traverse_raw.h"
#include "progress.h"

namespace metais {

    void prepass(std::string tag, const DirectoryLayout& layout, PrepassResult& prepass_result, bool skip_bad_json) {
        AttributeCatalog* cat = nullptr;
        auto& dct = prepass_result.dict;

        std::filesystem::path pages_dir;
        if ((tag == "node") || (tag == "nodes") || (tag == "entity") || (tag == "entities")) {
            pages_dir = layout.raw_nodes_dir / "pages";
            tag = "nodes";
            cat = &prepass_result.attrs_ent;
        }
        else {
            pages_dir = layout.raw_rels_dir / "pages";
            tag = "rels";
            cat = &prepass_result.attrs_rel;
        }
        const auto shards = list_shards_by_meta(pages_dir, tag);

        ProgressBar shard_bar(tag + " shards", shards.size());

        std::size_t last_shard = (std::size_t)-1;

        for (auto&& rec : ndjson_json_range(pages_dir, tag, skip_bad_json)) {
            ++prepass_result.total_records;
            if (rec.shard_index != last_shard) {
                last_shard = rec.shard_index;
                shard_bar.update(rec.shard_index + 1);
                //std::cout << "  (offset=" << rec.shard_offset << ")\n";
            }

            const auto& j = rec.obj;

            if (!j.contains("type") || !j["type"].is_string()) {
                ++prepass_result.missing_type;
                continue;
            }
            const std::string citype = j["type"].get<std::string>();
            cat->note_entity(citype);

            if (j.contains("attributes") && j["attributes"].is_array()) {
                for (const auto& a : j["attributes"]) {
                    if (!a.is_object()) {
                        ++prepass_result.bad_attributes_type;
                        continue;
                    }
                    if (!a.contains("name") || !a["name"].is_string()) continue;
                    cat->note_attr(citype, a["name"].get<std::string>());
                    if (!a.contains("value")) continue;
                    dct.note(a["value"].dump());
                }
            }
            else ++prepass_result.missing_attributes;
        }
        shard_bar.finish();
    }

}