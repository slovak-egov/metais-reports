#pragma once
#include <filesystem>
#include <string_view>

namespace metais {

    inline constexpr int kShardPad = 9;

    std::filesystem::path shard_data_path(const std::filesystem::path& pages_dir,
                                        std::string_view base,
                                        long offset);

    std::filesystem::path shard_meta_path(const std::filesystem::path& pages_dir,
                                        std::string_view base,
                                        long offset);

    std::filesystem::path shard_error_path(const std::filesystem::path& errs_dir,
                                        std::string_view base,
                                        long offset);

}