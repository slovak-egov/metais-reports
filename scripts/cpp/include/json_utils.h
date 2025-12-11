#pragma once
#include <nlohmann/json.hpp>
#include <string>
#include <fstream>
#include <stdexcept>

namespace metais {

using json = nlohmann::json;

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

// Normalize MetaIS-style responses:
//
// - if object with "result" (array) → return j["result"]
// - else if object with "results" (array) → return j["results"]
// - else if array → return j
// - else → return empty []
inline json extract_result_array(const json& j) {
    if (j.is_object()) {
        auto it = j.find("result");
        if (it != j.end() && it->is_array()) {
            return *it;
        }
        it = j.find("results");
        if (it != j.end() && it->is_array()) {
            return *it;
        }
    }

    if (j.is_array()) {
        return j;
    }

    return json::array();
}

}