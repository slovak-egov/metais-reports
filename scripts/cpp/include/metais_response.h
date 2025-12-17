#pragma once
#include <string>
#include <nlohmann/json.hpp>

namespace metais {
    using json = nlohmann::json;

    // Parse HTTP body into JSON, throw with a tag-rich message on failure.
    json parse_json_or_throw(const std::string& body, const std::string& tag);

    // Given parsed JSON, validate MetaIS error object shape and extract result array.
    json extract_results_array_or_throw(const json& j, const std::string& tag);

    // Convenience: parse + validate + extract result array.
    json parse_results_or_throw(const std::string& body, const std::string& tag);

    json parse_json_or_throw(std::string_view body, const std::string& tag);
}