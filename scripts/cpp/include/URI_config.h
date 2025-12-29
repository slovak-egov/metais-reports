#pragma once
#include <string>
#include <filesystem>
#include <iostream>
#include <cstdlib>

#include "json_utils.h"

namespace metais {

    namespace fs = std::filesystem;
    using json = nlohmann::json;

    inline std::string replace_all(std::string s, const std::string& from, const std::string& to) {
        if (from.empty()) return s;
        std::size_t pos = 0;
        while ((pos = s.find(from, pos)) != std::string::npos) {
            s.replace(pos, from.size(), to);
            pos += to.size();
        }
        return s;
    }

    inline std::string join_base_and_path(const std::string& base, const std::string& path) {
        if (path.empty()) return base;
        if (base.empty()) return path;
        if (base.back() == '/' && path.front() == '/') return base + path.substr(1);
        if (base.back() != '/' && path.front() != '/') return base + "/" + path;
        return base + path;
    }

    // ---------------------------------------------------------
    // URIConfig: holds base_url + all endpoint URLs
    // ---------------------------------------------------------
    struct URIConfig {
        std::string meta_instance;   // "prod" or "test"
        std::string base_url;        // "https://metais.slovensko.sk"

        // Paths (from JSON or defaults), without host
        std::string enum_list_path;
        std::string enum_detail_path_tpl;   // contains {name}

        std::string codelist_headers_list_path;
        std::string codelist_items_path_tpl; // contains {name}

        std::string citype_list_path;
        std::string citype_detail_path_tpl; // contains {name}

        std::string reltype_list_path;
        std::string reltype_detail_path_tpl; // contains {name}

        std::string report_run_path;

        // Convenience full URLs
        std::string enum_list_url() const {
            return join_base_and_path(base_url, enum_list_path);
        }
        std::string enum_detail_url(const std::string& name) const {
            return join_base_and_path(base_url, replace_all(enum_detail_path_tpl, "{name}", name));
        }
        std::string enum_detail_url_tpl() const {
            return join_base_and_path(base_url, enum_detail_path_tpl);
        }


        std::string citype_list_url() const {
            return join_base_and_path(base_url, citype_list_path);
        }
        std::string citype_detail_url(const std::string& name) const {
            return join_base_and_path(base_url, replace_all(citype_detail_path_tpl, "{name}", name));
        }
        std::string citype_detail_url_tpl() const {
            return join_base_and_path(base_url, citype_detail_path_tpl);
        }


        std::string reltype_list_url() const {
            return join_base_and_path(base_url, reltype_list_path);
        }
        std::string reltype_detail_url(const std::string& name) const {
            return join_base_and_path(base_url, replace_all(reltype_detail_path_tpl, "{name}", name));
        }
        std::string reltype_detail_url_tpl() const {
            return join_base_and_path(base_url, reltype_detail_path_tpl);
        }


        std::string codelist_headers_list_url() const {
            return join_base_and_path(base_url, codelist_headers_list_path);
        }
        std::string codelist_items_url(const std::string& code) const {
            return join_base_and_path(base_url, replace_all(codelist_items_path_tpl, "{name}", code));
        }
        std::string codelist_items_url_tpl() const {
            return join_base_and_path(base_url, codelist_items_path_tpl);
        }



        std::string report_run_url() const {
            return join_base_and_path(base_url, report_run_path);
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

        cfg.enum_list_path        = "api/enums-repo/enums/list";
        cfg.enum_detail_path_tpl  = "api/enums-repo/enums/enum/valid/{name}";

        cfg.citype_list_path      = "api/types-repo/citypes/list";
        cfg.citype_detail_path_tpl= "api/types-repo/citypes/citype/{name}";

        cfg.reltype_list_path     = "api/types-repo/relationshiptypes/list";
        cfg.reltype_detail_path_tpl= "api/types-repo/relationshiptypes/relationshiptype/{name}";

        cfg.codelist_headers_list_path =
            "api/codelist-repo/codelists/codelistheaders?language=sk&pageNumber=1&perPage=1000";
        cfg.codelist_items_path_tpl =
            "api/codelist-repo/codelists/codelistheaders/{name}/codelistitems?language=sk&pageNumber=1&perPage=10000";

        cfg.report_run_path            = "api/report/reports/run?lang=sk";

        try {
            json j = load_json_file(uri_json_path.string());

            // meta-instance
            if (j.contains("meta-instance") && j["meta-instance"].is_string()) {
                cfg.meta_instance = j["meta-instance"].get<std::string>();
            }


            
            // override individual paths if present
            if (j.contains("enum_list"))   cfg.enum_list_path = j["enum_list"].get<std::string>();
            if (j.contains("enum_detail")) cfg.enum_detail_path_tpl = j["enum_detail"].get<std::string>();


            if (j.contains("citype_list"))   cfg.citype_list_path = j["citype_list"].get<std::string>();
            if (j.contains("citype_detail")) cfg.citype_detail_path_tpl = j["citype_detail"].get<std::string>();


            if (j.contains("reltype_list"))   cfg.reltype_list_path = j["reltype_list"].get<std::string>();
            if (j.contains("reltype_detail")) cfg.reltype_detail_path_tpl = j["reltype_detail"].get<std::string>();


            if (j.contains("codelist_headers_list"))
                cfg.codelist_headers_list_path = j["codelist_headers_list"].get<std::string>();
            if (j.contains("codelist_items"))
                cfg.codelist_items_path_tpl = j["codelist_items"].get<std::string>();


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

}