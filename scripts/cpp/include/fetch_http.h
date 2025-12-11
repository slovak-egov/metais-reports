#pragma once
#include <string>
#include <nlohmann/json.hpp>

namespace http {

    std::string GET(const std::string& url, const std::string& bearer_token = "");

    nlohmann::json GET_json(const std::string& url, const std::string& bearer_token = "");

}