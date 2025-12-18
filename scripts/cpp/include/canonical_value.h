#pragma once

#include <string>
#include <nlohmann/json.hpp>

namespace metais {
    inline std::string canonical_value(const nlohmann::json& v) {
        if (v.is_string()) return v.get<std::string>();
        else return v.dump();
    }
}