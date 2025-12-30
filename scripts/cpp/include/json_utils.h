#pragma once
#include <nlohmann/json.hpp>
#include <string>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <filesystem>

namespace metais {

    namespace fs = std::filesystem;
    using json = nlohmann::json;

    inline constexpr std::size_t kMaxJsonPreview = 200;

    // Load a JSON file from disk into a nlohmann::json object.
    inline json load_json_file(const std::string& filepath) {
        std::ifstream f(filepath);
        if (!f.is_open()) {
            throw std::runtime_error("Cannot open JSON file: " + filepath);
        }

        try {
            return json::parse(f);
        } catch (const std::exception& e) {
            throw std::runtime_error(
                "Failed to parse JSON from file " + filepath + ": " + e.what()
            );
        }
    }

    inline json load_json_file(const fs::path& p) {
        return load_json_file(p.string());
    }

    // Normalize MetaIS-style responses:
    //
    // - if object with "result" (array) -> return j["result"]
    // - else if object with "results" (array) -> return j["results"]
    // - else if array -> return j
    // - else -> throw an error, it's not what we expected
    inline json extract_result_array(const json& j) {
        if (j.is_object()) {
            if (auto it = j.find("result"); it != j.end() && it->is_array()) return *it;
            if (auto it = j.find("results"); it != j.end() && it->is_array()) return *it;

            std::string preview;
            try { preview = j.dump(); } catch (...) { preview = "<dump failed>"; }
            if (preview.size() > kMaxJsonPreview) preview.resize(kMaxJsonPreview);

            throw std::runtime_error(
                "[JSON-extract_result_array] object without \"result\"/\"results\" arrays. preview: " + preview
            );
        }

        if (j.is_array()) return j;

        std::string preview;
        try { preview = j.dump(); } catch (...) { preview = "<dump failed>"; }
        if (preview.size() > kMaxJsonPreview) preview.resize(kMaxJsonPreview);

        throw std::runtime_error(
            "[JSON-extract_result_array] expected array or object with \"result\"/\"results\" arrays; got type=" +
            std::string(j.type_name()) + " preview: " + preview
        );
    }

}