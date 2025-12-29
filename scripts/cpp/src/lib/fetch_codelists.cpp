#include "fetch_codelists.h"

#include "fetch_http.h"
#include "step_marker.h"

#include <nlohmann/json.hpp>
#include <fstream>
#include <iostream>

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace {

    static std::vector<std::string> extract_valid_codes_from_headers(const json& headers_doc) {
        std::vector<std::string> codes;

        if (!headers_doc.is_object()) return codes;
        if (!headers_doc.contains("codelists") || !headers_doc["codelists"].is_array()) return codes;

        for (const auto& it : headers_doc["codelists"]) {
            if (!it.is_object()) continue;

            // If there is a "valid" field and you want valid-only, enforce it.
            // If there isn't, this will default to true.
            const bool valid = it.value("valid", true);
            if (!valid) continue;

            const std::string code = it.value("code", "");
            if (!code.empty()) codes.push_back(code);
        }
        return codes;
    }

    static void write_pretty_json(const fs::path& p, const json& j) {
        std::ofstream out(p);
        if (!out.is_open()) {
            throw std::runtime_error("Failed to write " + p.string());
        }
        out << j.dump(2);
    }

}

namespace metais {

    void fetch_codelists(const DirectoryLayout& layout,
                        const URIConfig& uri_cfg,
                        const HTTPConfig& http_cfg)
    {
        fs::path root = layout.codelists_root;

        if (is_done(root)) {
            std::cout << "[CODELISTS] .done marker present in " << root << " - skipping.\n";
            return;
        }

        // Open endpoints: no auth
        HTTPConfig open_cfg = http_cfg;
        open_cfg.auth.mode = "none";
        open_cfg.auth.required = false;

        // Ensure dirs exist (layout.create_fetch_dirs() already makes them, but be robust)
        fs::create_directories(layout.codelists_root);
        fs::create_directories(layout.codelists_items_dir);

        // 1) Fetch headers list
        const std::string list_url = uri_cfg.codelist_headers_list_url();
        json headers_doc = http::GET_json(list_url, open_cfg);

        write_pretty_json(layout.codelists_headers_json, headers_doc);
        std::cout << "[CODELISTS] Saved headers -> " << layout.codelists_headers_json << "\n";

        // 2) Extract codes
        auto codes = extract_valid_codes_from_headers(headers_doc);
        std::cout << "[CODELISTS] Will fetch " << codes.size() << " codelists.\n";

        // 3) Fetch items for each code
        for (const std::string& code : codes) {
            const std::string url = uri_cfg.codelist_items_url(code);
            json items_doc = http::GET_json(url, open_cfg);

            const fs::path out_path = layout.codelists_items_dir / (code + ".json");
            write_pretty_json(out_path, items_doc);

            std::cout << "[CODELISTS] " << code << " -> " << out_path << "\n";
        }

        mark_done(root);
        std::cout << "[CODELISTS] Done.\n";
    }

}