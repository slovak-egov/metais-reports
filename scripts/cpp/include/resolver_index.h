#pragma once
#include "binary_formats.h"
#include <vector>
#include <fstream>

namespace metais {

    class GlobalResolverIndex {
    public:
        void load(const fs::path& resolver_bin, std::size_t expected_rows) {
            std::uint64_t bytes = fs::file_size(resolver_bin);
            if (bytes % 6 != 0) throw std::runtime_error("resolver.bin not multiple of 6");
            std::size_t rows = (std::size_t)(bytes / 6);
            if (expected_rows != 0 && rows != expected_rows) {
                throw std::runtime_error("resolver.bin rows != uuids.bin rows");
            }

            citype_index_.resize(rows);

            std::ifstream is(resolver_bin, std::ios::binary);
            if (!is) throw std::runtime_error("open failed: " + resolver_bin.string());

            for (std::size_t i = 0; i < rows; ++i) {
                CitypeIndex ci = read_u16_le(is);
                (void)read_u32_le(is); // local_index (not needed here)
                citype_index_[i] = ci;
            }
        }

        CitypeIndex citype_index_of(GlobalId gid) const {
            return citype_index_.at((std::size_t)gid);
        }

        std::size_t size() const { return citype_index_.size(); }

    private:
        std::vector<CitypeIndex> citype_index_;
    };

}