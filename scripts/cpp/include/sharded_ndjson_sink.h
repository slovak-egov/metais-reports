#pragma once
#include "page_sink.h"

#include <filesystem>
#include <fstream>
#include <string>
#include <nlohmann/json.hpp>

namespace metais {

    class ShardedNdjsonSink : public PageSink {
    public:
        ShardedNdjsonSink(
            std::filesystem::path pages_dir,
            std::string base_name
        );

        void begin_page(long offset, int limit) override;
        void write_item(const nlohmann::json& obj) override;
        void end_page(std::size_t n) override;

    private:
        std::filesystem::path pages_dir_;
        std::string base_name_;

        long current_offset_ = -1;
        std::filesystem::path tmp_path_;
        std::filesystem::path final_path_;
        std::ofstream out_;
    };

}
