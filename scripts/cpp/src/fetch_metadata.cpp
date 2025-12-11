#include "../include/fetch_metadata.h"
#include "../include/json_utils.h"
#include "../include/fetch_http.h"
#include "../include/step_marker.h"

#include <filesystem>
#include <fstream>
#include <iostream>

namespace metais {

    using json = nlohmann::json;
    namespace fs = std::filesystem;

    // -----------------------------
    // CITYPES
    // -----------------------------

    std::vector<std::string>
    fetch_citype_list(const DirectoryLayout& layout, const URIConfig& uri_cfg)
    {
        const std::string list_url = uri_cfg.citype_list_url();
        json raw = extract_result_array(http::GET_json(list_url, ""));

        std::cout << "[META] Citype list: received " << raw.size()
                  << " raw entries from " << list_url << "\n";

        // Save full list for debugging / parity with Python:
        fs::path list_path = layout.metadata_root / "citypes_list.json";
        {
            std::ofstream out(list_path);
            if (!out.is_open()) {
                throw std::runtime_error(
                    "Failed to write citypes_list.json at " + list_path.string()
                );
            }
            out << raw.dump(2);
        }
        std::cout << "[META] Saved citypes list -> " << list_path << "\n";

        // Extract technicalName / name / code
        std::vector<std::string> citypes;
        citypes.reserve(raw.size());

        for (const auto& item : raw) {
            if (!item.is_object()) continue;

            std::string code;
            if (item.contains("technicalName") && item["technicalName"].is_string()) {
                code = item["technicalName"].get<std::string>();
            } else if (item.contains("name") && item["name"].is_string()) {
                code = item["name"].get<std::string>();
            } else if (item.contains("code") && item["code"].is_string()) {
                code = item["code"].get<std::string>();
            }

            if (!code.empty()) {
                citypes.push_back(code);
            }
        }

        return citypes;
    }

    void fetch_citype_detail(const DirectoryLayout& layout,
                             const URIConfig& uri_cfg,
                             const std::string& citype_code)
    {
        const std::string url = uri_cfg.citype_detail_base_url() + "/" + citype_code;
        json detail = http::GET_json(url, "");

        fs::path out_dir = layout.nodes_meta_dir;
        std::error_code ec;
        fs::create_directories(out_dir, ec);
        if (ec) {
            throw std::runtime_error(
                "Failed to create nodes_meta_dir '" + out_dir.string() +
                "': " + ec.message()
            );
        }

        fs::path out_path = out_dir / (citype_code + ".json");
        std::ofstream out(out_path);
        if (!out.is_open()) {
            throw std::runtime_error(
                "Failed to write citype meta '" + citype_code +
                "' at " + out_path.string()
            );
        }

        out << detail.dump(2);
        out.close();

        std::cout << "[META] Citype " << citype_code
                  << " -> " << out_path << "\n";
    }

    // -----------------------------
    // RELTYPES
    // -----------------------------

    std::vector<std::string>
    fetch_reltype_list(const DirectoryLayout& layout, const URIConfig& uri_cfg)
    {
        const std::string list_url = uri_cfg.reltype_list_url();
        json raw = extract_result_array(http::GET_json(list_url, ""));

        std::cout << "[META] Reltype list: received " << raw.size()
                  << " raw entries from " << list_url << "\n";

        // Save full list
        fs::path list_path = layout.metadata_root / "reltypes_list.json";
        {
            std::ofstream out(list_path);
            if (!out.is_open()) {
                throw std::runtime_error(
                    "Failed to write reltypes_list.json at " + list_path.string()
                );
            }
            out << raw.dump(2);
        }
        std::cout << "[META] Saved reltypes list -> " << list_path << "\n";

        std::vector<std::string> reltypes;
        reltypes.reserve(raw.size());

        for (const auto& item : raw) {
            if (!item.is_object()) continue;

            std::string code;
            if (item.contains("technicalName") && item["technicalName"].is_string()) {
                code = item["technicalName"].get<std::string>();
            } else if (item.contains("name") && item["name"].is_string()) {
                code = item["name"].get<std::string>();
            } else if (item.contains("code") && item["code"].is_string()) {
                code = item["code"].get<std::string>();
            }

            if (!code.empty()) {
                reltypes.push_back(code);
            }
        }

        return reltypes;
    }

    void fetch_reltype_detail(const DirectoryLayout& layout,
                              const URIConfig& uri_cfg,
                              const std::string& reltype_code)
    {
        const std::string url = uri_cfg.reltype_detail_base_url() + "/" + reltype_code;
        json detail = http::GET_json(url, "");

        fs::path out_dir = layout.rels_meta_dir;
        std::error_code ec;
        fs::create_directories(out_dir, ec);
        if (ec) {
            throw std::runtime_error(
                "Failed to create rels_meta_dir '" + out_dir.string() +
                "': " + ec.message()
            );
        }

        fs::path out_path = out_dir / (reltype_code + ".json");
        std::ofstream out(out_path);
        if (!out.is_open()) {
            throw std::runtime_error(
                "Failed to write reltype meta '" + reltype_code +
                "' at " + out_path.string()
            );
        }

        out << detail.dump(2);
        out.close();

        std::cout << "[META] Reltype " << reltype_code
                  << " -> " << out_path << "\n";
    }

    // -----------------------------
    // Orchestrator
    // -----------------------------

    void fetch_metadata(const DirectoryLayout& layout, const URIConfig& uri_cfg)
    {
        fs::path meta_root = layout.metadata_root;

        if (is_done(meta_root)) {
            std::cout << "[META] .done marker present in " << meta_root << " - skipping." << std::endl;
            return;
        }
        // CITYPES
        {
            fs::path nodes_meta_root = layout.nodes_meta_dir;
            if (is_done(nodes_meta_root)) {
                std::cout << "[META] .done present in " << nodes_meta_root
                          << " – skipping citype metadata.\n";
            } else {
                auto citypes = fetch_citype_list(layout, uri_cfg);
                std::cout << "[META] Will fetch metadata for "
                          << citypes.size() << " citypes.\n";

                for (const auto& code : citypes) {
                    fetch_citype_detail(layout, uri_cfg, code);
                }

                mark_done(nodes_meta_root);
            }
        }

        // RELTYPES
        {
            fs::path rels_meta_root = layout.rels_meta_dir;
            if (is_done(rels_meta_root)) {
                std::cout << "[META] .done present in " << rels_meta_root
                          << " – skipping reltype metadata.\n";
            } else {
                auto reltypes = fetch_reltype_list(layout, uri_cfg);
                std::cout << "[META] Will fetch metadata for "
                          << reltypes.size() << " reltypes.\n";

                for (const auto& code : reltypes) {
                    fetch_reltype_detail(layout, uri_cfg, code);
                }

                mark_done(rels_meta_root);
            }
        }
        mark_done(meta_root);
    }

}