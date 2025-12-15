#include "../include/fetch_enums.h"
#include "../include/json_utils.h"
#include "../include/fetch_http.h"
#include "../include/step_marker.h"
#include "../include/fetch_open.h"

#include <fstream>
#include <iostream>
#include <unordered_map>

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace {

    std::optional<std::string> extract_enum_code_valid_only(const json& item) {
        if (!item.value("valid", false)) return std::nullopt;
        auto code = item.value("code", "");
        if (code.empty()) return std::nullopt;
        return code;
    }

    void merge_enum_items_into(
        const std::string& enum_name,
        const json& enum_items, // array of {code,value,...}
        std::map<std::string, std::vector<std::string>>& enum_merged // map of key: [enum1 where the key appeared, value1, enum2 where the key appeared, value2, ...] <- even length
    ) {
        if (!enum_items.is_array()) {
            throw std::runtime_error(
                "[ENUMS] Expected enumItems array for " + enum_name +
                ", got type=" + std::string(enum_items.type_name())
            );
        }

        for (const auto& enum_item : enum_items) {
            if (!enum_item.is_object()) continue;

            const std::string enum_key   = enum_item.value("code", "");
            const std::string enum_value = enum_item.value("value", "");

            if (enum_key.empty()) {
                std::cout << "[WARNING] Empty string in enum key. Skipping";
                continue;
            }

            auto& vec = enum_merged[enum_key];
            vec.push_back(enum_name);
            vec.push_back(enum_value);
        }
    }

}

namespace metais {

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

    void fetch_enums(const DirectoryLayout& dir_layout,
                const URIConfig& uri_cfg,
                const HTTPConfig& http_cfg) {

        fs::path enums_root = dir_layout.enums_root;

        if (is_done(enums_root)) {
            std::cout << "[ENUMS] .done marker present in " << enums_root << " - skipping." << std::endl;
            return;
        }

        HTTPConfig open_cfg = http_cfg;
        open_cfg.auth.mode = "none";
        open_cfg.auth.required = false;

        OpenFetchingSpec s;
        s.out_dir = dir_layout.enums_root;
        s.out_filename = "enums_list.json";
        s.list_url = uri_cfg.enum_list_url();
        s.base_url = uri_cfg.enum_detail_base_url();
        s.tag = "ENUM";
        s.kind = "Enum";
        s.label = "Enum list";
        s.strict_mkdir = true;
        s.transform = [](const json& d){
            return d.value("enumItems", json::array());
        };

        auto enum_codes = fetch_element_list(s, open_cfg, extract_enum_code_valid_only);

        s.out_dir = dir_layout.enums_root / "valid";
        s.out_filename = "";
        std::map<std::string, std::vector<std::string>> enum_merged;
        for (const std::string& enum_name : enum_codes) {
            json enum_items = fetch_detail(enum_name, open_cfg, s);
            merge_enum_items_into(enum_name, enum_items, enum_merged);
        }

        auto enum_collisions = handle_merged_enums(enum_merged);

        json merged_js = json::object();
        for (const auto& kv : enum_merged) {
            const std::string& key = kv.first;
            const std::vector<std::string>& vec = kv.second;
            if (vec.size() >= 2) {
                const std::string& value = vec[1];
                merged_js[key] = value;
            }
        }

        fs::path merged_path = dir_layout.enums_root / "enums_merged.json";
        std::ofstream out(merged_path);
        if (!out.is_open()) {
            throw std::runtime_error(
                "Failed to write enums_merged.json at " + merged_path.string()
            );
        }
        out << merged_js.dump(2);
        out.close();

        std::cout << "[ENUMS] Saved enums_merged.json -> "
                  << merged_path << "\n";

        if (!enum_collisions.empty()) {
            nlohmann::json collisions_js = nlohmann::json::array();

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
                collisions_js.push_back(entry);
            }

            fs::path collisions_path = dir_layout.enums_root / "enums_collisions.json";
            std::ofstream out(collisions_path);
            if (!out.is_open()) {
                throw std::runtime_error(
                    "Failed to write enums_collisions.json at " + collisions_path.string()
                );
            }
            out << collisions_js.dump(2);
            out.close();

            std::cout << "[ENUMS] Saved enums_collisions.json -> "
                    << collisions_path << "\n";
        }
        
        mark_done(enums_root);
    }

}