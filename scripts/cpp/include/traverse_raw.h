#pragma once
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>
#include <vector>
#include <fstream>
#include <nlohmann/json.hpp>

namespace metais {

    using json = nlohmann::json;

    struct ShardInfo {
        long offset;
        std::filesystem::path ndjson_path;
        std::filesystem::path meta_path;
    };

    std::optional<long> parse_offset_from_meta_filename(std::string_view fname, std::string_view base);
    std::vector<ShardInfo> list_shards_by_meta(const std::filesystem::path& pages_dir, std::string_view base);

    // Range record
    struct NdjsonJsonRecord {
        json obj;
        long shard_offset = 0;
        std::size_t line_no = 0;

        std::size_t shard_index = 0;
        std::size_t shard_count = 0;
    };

    // Range type
    class NdjsonJsonRange {
    public:
        NdjsonJsonRange(std::filesystem::path pages_dir, std::string base, bool skip_bad_json);

        class iterator {
        public:
            using iterator_category = std::input_iterator_tag;
            using value_type        = NdjsonJsonRecord;
            using difference_type   = std::ptrdiff_t;
            using pointer           = const NdjsonJsonRecord*;
            using reference         = const NdjsonJsonRecord&;

            iterator();
            explicit iterator(const NdjsonJsonRange* owner);

            reference operator*() const;
            pointer operator->() const;
            iterator& operator++();

            bool operator==(const iterator& other) const;
            bool operator!=(const iterator& other) const;

        private:
            void open_current_shard_or_end();
            void advance_or_end();

            const NdjsonJsonRange* owner_ = nullptr;
            std::size_t shard_i_ = 0;

            std::ifstream in_;     // <-- this is the missing one
            std::string line_;     // used by getline + parse
            std::size_t line_no_ = 0;

            NdjsonJsonRecord cur_{};

            bool at_end_ = false;
        };

        iterator begin() const;
        iterator end() const;

    private:
        std::filesystem::path pages_dir_;
        std::string base_;
        bool skip_bad_json_ = false;
        std::vector<ShardInfo> shards_;

        friend class iterator;
    };

    inline NdjsonJsonRange ndjson_json_range(const std::filesystem::path& pages_dir,
                                            std::string_view base,
                                            bool skip_bad_json = false) {
        return NdjsonJsonRange(pages_dir, std::string(base), skip_bad_json);
    }

}