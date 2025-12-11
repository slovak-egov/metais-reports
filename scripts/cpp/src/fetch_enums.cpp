#include "../include/fetch_enums.h"
#include "../include/json_utils.h"
#include "../include/fetch_http.h"
#include "../include/step_marker.h"
#include "../include/URI_config.h"

#include <fstream>
#include <iostream>
#include <unordered_map>

namespace metais {

    using json = nlohmann::json;
    namespace fs = std::filesystem;

    std::vector<std::string> fetch_enum_list(const DirectoryLayout& dir_layout, const URIConfig& uri_cfg) {

        std::string enum_list_uri = uri_cfg.enum_list_url();

        json enum_list = extract_result_array(http::GET_json(enum_list_uri, ""));

        std::cout << "[ENUMS] received " << enum_list.size()
                  << " raw entries from " << enum_list_uri << "\n";

        fs::path list_path = dir_layout.enums_root / "enums_list.json";
        
        std::ofstream out(list_path);
        if (!out.is_open()) {
            throw std::runtime_error("Failed to write enums_list.json at " + list_path.string());
        }

        out << enum_list.dump(2);
        out.close();

        std::cout << "[ENUMS] Saved enum list -> " << list_path << "\n";

        std::vector<std::string> enum_codes;
        enum_codes.reserve(enum_list.size());

        for (const auto& item : enum_list) {
            if (!item.is_object()) continue;

            bool valid = item.value("valid", false);
            if (!valid) continue;

            std::string code = item.value("code", "");
            if (code.empty()) continue;

            enum_codes.push_back(code);
        }

        return enum_codes;
    }

    void fetch_enum(const DirectoryLayout& layout,
                const URIConfig& uri_cfg,
                const std::string& enum_name,
                std::map<std::string, std::vector<std::string>>& enum_merged) {

        std::string enum_uri = uri_cfg.enum_detail_base_url() + "/" + enum_name;
        json detail = http::GET_json(enum_uri, "");
        json enum_items = detail.value("enumItems", json::array());

        std::cout << "[ENUM] received " << enum_name
                  << " from " << enum_uri << "\n";

        fs::path valid_dir = layout.enums_root / "valid";
        std::error_code ec;
        fs::create_directories(valid_dir, ec);

        fs::path enum_path = valid_dir / (enum_name + ".json");
        
        std::ofstream out(enum_path);
        if (!out.is_open()) {
            throw std::runtime_error("Failed to write " + enum_name + ".json at " + enum_path.string());
        }

        out << enum_items.dump(2);
        out.close();

        for (auto& enum_item : enum_items) {
            std::string enum_key = enum_item.value("code", "");
            std::string enum_value = enum_item.value("value", "");

            if (enum_key.empty()) {
                std::cout << "[WARNING] Empty string in enum key. Skipping";
                continue;
            }

            auto& vec = enum_merged[enum_key];
            vec.push_back(enum_name); // to track where this one came from
            vec.push_back(enum_value);
        }
    }

    std::map<std::string, std::vector<std::string>>
    handle_merged_enums(std::map<std::string, std::vector<std::string>>& enum_merged) {

        std::map<std::string, std::vector<std::string>> enum_collisions;

        for (auto& kv : enum_merged) {
            const std::string& key = kv.first;
            std::vector<std::string>& vec = kv.second;

            if (vec.empty()) {
                continue;
            }

            // vec is [enum1, val1, enum2, val2, ...]
            if (vec.size() % 2 != 0) {
                std::cerr << "[ENUMS] WARNING: odd-length value array for key '"
                        << key << "' (size=" << vec.size() << "). "
                        << "Ignoring last dangling entry.\n";
                vec.pop_back();
            }

            const std::size_t pair_count = vec.size() / 2;
            if (pair_count == 0) {
                continue;
            }

            if (pair_count > 1) {
                // Record full chain of (enum, value) pairs for this key
                enum_collisions[key] = vec;
            }

            // Keep only the last (enum, value) pair in enum_merged
            const std::string last_enum  = vec[vec.size() - 2];
            const std::string last_value = vec[vec.size() - 1];

            vec.clear();
            vec.push_back(last_enum);
            vec.push_back(last_value);
        }

        return enum_collisions;
    }

    void fetch_enums(const DirectoryLayout& dir_layout, const URIConfig& uri_cfg) {

        fs::path enums_root = dir_layout.enums_root;

        if (is_done(enums_root)) {
            std::cout << "[ENUMS] .done marker present in " << enums_root << " - skipping." << std::endl;
            return;
        }

        std::vector<std::string> enum_codes = fetch_enum_list(dir_layout, uri_cfg);

        std::map<std::string, std::vector<std::string>> enum_merged;
        for (const std::string& enum_name : enum_codes) {
            fetch_enum(dir_layout, uri_cfg, enum_name, enum_merged);
        }

        auto enum_collisions = handle_merged_enums(enum_merged);

        json merged_json = json::object();
        for (const auto& kv : enum_merged) {
            const std::string& key = kv.first;
            const std::vector<std::string>& vec = kv.second;
            if (vec.size() >= 2) {
                const std::string& value = vec[1];
                merged_json[key] = value;
            }
        }

        fs::path merged_path = dir_layout.enums_root / "enums_merged.json";
        std::ofstream out(merged_path);
        if (!out.is_open()) {
            throw std::runtime_error(
                "Failed to write enums_merged.json at " + merged_path.string()
            );
        }
        out << merged_json.dump(2);
        out.close();

        std::cout << "[ENUMS] Saved enums_merged.json -> "
                  << merged_path << "\n";

        if (!enum_collisions.empty()) {
            nlohmann::json collisions_json = nlohmann::json::array();

            for (const auto& kv : enum_collisions) {
                const std::string& key = kv.first;
                const std::vector<std::string>& vec = kv.second;

                // vec = [enum1, val1, enum2, val2, ...]
                nlohmann::json entry;
                entry["item_code"] = key;

                nlohmann::json sources = nlohmann::json::array();
                for (std::size_t i = 0; i + 1 < vec.size(); i += 2) {
                    nlohmann::json src;
                    src["enum"]  = vec[i];
                    src["value"] = vec[i + 1];
                    sources.push_back(src);
                }

                entry["sources"] = sources;
                collisions_json.push_back(entry);
            }

            fs::path collisions_path = dir_layout.enums_root / "enums_collisions.json";
            std::ofstream out(collisions_path);
            if (!out.is_open()) {
                throw std::runtime_error(
                    "Failed to write enums_collisions.json at " + collisions_path.string()
                );
            }
            out << collisions_json.dump(2);
            out.close();

            std::cout << "[ENUMS] Saved enums_collisions.json -> "
                    << collisions_path << "\n";
        }
        
        mark_done(enums_root);
    }

}