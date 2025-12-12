#pragma once
#include <string>
#include <filesystem>
#include <iostream>
#include <cstdlib>

#include "json_utils.h"

namespace metais {

namespace fs = std::filesystem;
using json = nlohmann::json;

// ---------------------------------------------------------
// URIConfig: holds base_url + all endpoint URLs
// ---------------------------------------------------------
struct URIConfig {
    std::string meta_instance;   // "prod" or "test"
    std::string base_url;        // e.g. "https://metais.slovensko.sk"

    // Paths (from JSON or defaults), without host
    std::string enum_list_path;
    std::string enum_detail_base_path;

    std::string citype_list_path;
    std::string citype_detail_base_path;

    std::string reltype_list_path;
    std::string reltype_detail_base_path;

    std::string report_run_path;

    // Convenience full URLs
    std::string enum_list_url() const {
        return base_url + "/" + enum_list_path;
    }
    std::string enum_detail_base_url() const {
        return base_url + "/" + enum_detail_base_path;
    }

    std::string citype_list_url() const {
        return base_url + "/" + citype_list_path;
    }
    std::string citype_detail_base_url() const {
        return base_url + "/" + citype_detail_base_path;
    }

    std::string reltype_list_url() const {
        return base_url + "/" + reltype_list_path;
    }
    std::string reltype_detail_base_url() const {
        return base_url + "/" + reltype_detail_base_path;
    }

    std::string report_run_url() const {
        return base_url + "/" + report_run_path;
    }
};

// Decide base_url from instance or env
inline std::string resolve_base_url(const std::string& instance_raw) {
    // allow env to override everything
    if (const char* env_url = std::getenv("METAIS_BASE_URL")) {
        return std::string(env_url);
    }

    std::string instance = instance_raw;
    if (const char* env_inst = std::getenv("METAIS_INSTANCE")) {
        instance = env_inst;
    }

    if (instance == "test") {
        return "https://metais-test.slovensko.sk";
    }

    // default: prod
    return "https://metais.slovensko.sk";
}

// Load from URI.json (if missing, use all defaults)
inline URIConfig load_uri_config(const fs::path& uri_json_path) {
    URIConfig cfg;

    // Defaults for paths (no host)
    cfg.meta_instance              = "prod";
    cfg.enum_list_path            = "api/enums-repo/enums/list";
    cfg.enum_detail_base_path      = "api/enums-repo/enums/enum/valid";

    cfg.citype_list_path           = "api/types-repo/citypes/list";
    cfg.citype_detail_base_path    = "api/types-repo/citypes/citype";

    cfg.reltype_list_path          = "api/types-repo/relationshiptypes/list";
    cfg.reltype_detail_base_path   = "api/types-repo/relationshiptypes/relationshiptype";

    cfg.report_run_path = "api/report/reports/run?lang=sk";

    try {
        json j = load_json_file(uri_json_path.string());

        // meta-instance
        if (j.contains("meta-instance") && j["meta-instance"].is_string()) {
            cfg.meta_instance = j["meta-instance"].get<std::string>();
        }


        
        // override individual paths if present
        if (j.contains("enum_list")) {
            cfg.enum_list_path = j["enum_list"].get<std::string>();
        }
        if (j.contains("enum_detail_base")) {
            cfg.enum_detail_base_path = j["enum_detail_base"].get<std::string>();
        }



        if (j.contains("citype_list")) {
            cfg.citype_list_path = j["citype_list"].get<std::string>();
        }
        if (j.contains("citype_detail_base")) {
            cfg.citype_detail_base_path = j["citype_detail_base"].get<std::string>();
        }



        if (j.contains("reltype_list")) {
            cfg.reltype_list_path = j["reltype_list"].get<std::string>();
        }
        if (j.contains("reltype_detail_base")) {
            cfg.reltype_detail_base_path = j["reltype_detail_base"].get<std::string>();
        }



        if (j.contains("apiuri") && j["apiuri"].is_string()) {
            cfg.report_run_path = j["apiuri"].get<std::string>();
        }

    } catch (const std::exception& e) {
        std::cerr << "[URI_config] WARNING: " << e.what()
                  << " - using default URIs.\n";
    }

    cfg.base_url = resolve_base_url(cfg.meta_instance);

    std::cout << "[URI_config] instance = " << cfg.meta_instance << "\n";
    std::cout << "[URI_config] base_url = " << cfg.base_url << "\n";

    return cfg;
}

// ---------------------------------------------------------
// TemplateConfig: Groovy templates (env + defaults)
// ---------------------------------------------------------
struct TemplateConfig {
    std::string node_template_all;
    std::string node_template_valid_only;
    std::string rel_template_all;
    std::string rel_template_valid_only;
};

inline TemplateConfig load_template_config() {
    TemplateConfig t;

    const char* nt_all = std::getenv("METAIS_NODE_TEMPLATE_ALL");
    const char* nt_val = std::getenv("METAIS_NODE_TEMPLATE_VALID_ONLY");
    const char* rt_all = std::getenv("METAIS_REL_TEMPLATE_ALL");
    const char* rt_val = std::getenv("METAIS_REL_TEMPLATE_VALID_ONLY");

    t.node_template_all        = nt_all ? nt_all : "groovy/template/node_template_all.groovy";
    t.node_template_valid_only = nt_val ? nt_val : "groovy/template/node_template_valid_only.groovy";

    t.rel_template_all         = rt_all ? rt_all : "groovy/template/relation_template_all.groovy";
    t.rel_template_valid_only  = rt_val ? rt_val : "groovy/template/relation_template_valid_only.groovy";

    return t;
}

} // namespace metais