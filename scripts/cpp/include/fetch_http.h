#pragma once
#include <string>
#include <stdexcept>
#include <nlohmann/json.hpp>

#include "http_config.h"

namespace http {

    struct HttpError : public std::runtime_error {
        long status;
        std::string url;
        std::string body;

        HttpError(long status_, std::string url_, std::string body_)
            : std::runtime_error("HTTP " + std::to_string(status_) + " for " + url_),
            status(status_),
            url(std::move(url_)),
            body(std::move(body_)) {}
    };

    // low-level (no retries, caller provides token)
    std::string GET(const std::string& url, const std::string& bearer_token = "");
    nlohmann::json GET_json(const std::string& url, const std::string& bearer_token = "");

    // high-level (uses HTTPConfig: auth + timeouts + retries)
    std::string GET(const std::string& url, const metais::HTTPConfig& settings);
    nlohmann::json GET_json(const std::string& url, const metais::HTTPConfig& settings);

}