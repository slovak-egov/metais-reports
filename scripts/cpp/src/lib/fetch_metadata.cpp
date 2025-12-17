#include "fetch_metadata.h"
#include "json_utils.h"
#include "fetch_http.h"
#include "step_marker.h"
#include "fetch_open.h"

#include <filesystem>
#include <fstream>
#include <iostream>

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace {
    
    std::optional<std::string> pick_first_string_field(
        const json& obj,
        std::initializer_list<const char*> keys
    ) {
        for (const char* k : keys) {
            auto it = obj.find(k);
            if (it != obj.end() && it->is_string()) {
                auto s = it->get<std::string>();
                if (!s.empty()) return s;
            }
        }
        return std::nullopt;
    }

    std::optional<std::string> extract_citype_code(const json& item) {
        return pick_first_string_field(item, {"technicalName", "name", "code"});
    }

    std::optional<std::string> extract_reltype_code(const json& item) {
        return pick_first_string_field(item, {"technicalName", "name", "code"});
    }
}

namespace metais {

    // -----------------------------
    // Orchestrator
    // -----------------------------

    void fetch_metadata(const DirectoryLayout& layout,
                        const URIConfig& uri_cfg,
                        const HTTPConfig& http_cfg)
    {
        fs::path meta_root = layout.metadata_root;

        if (is_done(meta_root)) {
            std::cout << "[META] .done marker present in " << meta_root << " - skipping." << std::endl;
            return;
        }

        HTTPConfig open_cfg = http_cfg;
        open_cfg.auth.mode = "none";
        open_cfg.auth.required = false;
        
        // CITYPES
        {
            fs::path nodes_meta_root = layout.nodes_meta_dir;
            if (is_done(nodes_meta_root)) {
                std::cout << "[META] .done present in " << nodes_meta_root
                          << " - skipping citype metadata.\n";
            } else {
                OpenFetchingSpec s;
                s.out_dir = layout.metadata_root;
                s.out_filename = "citypes_list.json";
                s.list_url = uri_cfg.citype_list_url();
                s.base_url = uri_cfg.citype_detail_base_url();
                s.tag = "META";
                s.kind = "Citype";
                s.label = "Citype list";
                s.strict_mkdir = true;

                auto citypes = fetch_element_list(s, open_cfg, extract_citype_code);
                std::cout << "[META] Will fetch metadata for "
                          << citypes.size() << " citypes.\n";

                s.out_dir = layout.nodes_meta_dir;
                s.out_filename = "";
                for (const auto& code : citypes) fetch_detail(code, open_cfg, s);

                mark_done(nodes_meta_root);
            }
        }

        // RELTYPES
        {
            fs::path rels_meta_root = layout.rels_meta_dir;
            if (is_done(rels_meta_root)) {
                std::cout << "[META] .done present in " << rels_meta_root
                          << " - skipping reltype metadata.\n";
            } else {
                OpenFetchingSpec s;
                s.out_dir = layout.metadata_root;
                s.out_filename = "reltypes_list.json";
                s.list_url = uri_cfg.reltype_list_url();
                s.base_url = uri_cfg.reltype_detail_base_url();
                s.tag = "META";
                s.kind = "Reltype";
                s.label = "Reltype list";
                s.strict_mkdir = true;

                auto reltypes = fetch_element_list(s, open_cfg, extract_reltype_code);
                std::cout << "[META] Will fetch metadata for "
                          << reltypes.size() << " reltypes.\n";

                s.out_dir = layout.rels_meta_dir;
                s.out_filename = "";
                for (const auto& code : reltypes) fetch_detail(code, open_cfg, s);

                mark_done(rels_meta_root);
            }
        }
        mark_done(meta_root);
    }

}