#pragma once
#include <string>

namespace metais {

    struct OIDCLoginResult {
        std::string access_token;
    };

    OIDCLoginResult oidc_login_userpass_pkce(
        const std::string& base_url,
        const std::string& authorize_path,
        const std::string& login_path,
        const std::string& token_path,
        const std::string& client_id,
        const std::string& redirect_uri,
        const std::string& scope,
        const std::string& username,
        const std::string& password,
        const std::string& user_agent
    );

}