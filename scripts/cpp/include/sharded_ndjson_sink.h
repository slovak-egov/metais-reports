#pragma once
#include "binary_sink.h"

#include <filesystem>
#include <fstream>
#include <string>
#include <nlohmann/json.hpp>

namespace metais {

    // Shared helper for deterministic shard naming (e.g., nodes.000000000.ndjson).
    std::filesystem::path shard_path(
        const std::filesystem::path& dir,
        const std::string& base,
        long offset
    );

    // Shared helper for atomic shard writes with temp + rename.
    void write_shard_ndjson(
        const std::filesystem::path& out_dir,
        const std::string& base,
        long offset,
        const nlohmann::json& arr
    );

    class ShardedNdjsonSink : public BinarySink {
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
