#pragma once

#include <string>
#include <nlohmann/json.hpp>

namespace metais {

    inline std::string canonical_value(const nlohmann::json& v) {
        // ensure_ascii=false keeps UTF-8 (no \uXXXX spam)
        return v.dump(-1, ' ', /*ensure_ascii=*/false);
    }
    
}