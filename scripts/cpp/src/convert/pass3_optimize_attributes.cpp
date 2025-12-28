#include "pass3_optimize_attributes.h"

#include "binary_formats.h"
#include "step_marker.h"

#include <nlohmann/json.hpp>

#include <filesystem>
#include <fstream>
#include <iostream>
#include <vector>
#include <string>
#include <stdexcept>
#include <cstdint>
#include <algorithm>
#include <optional>

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace {

    static json read_json_file(const fs::path& p) {
        std::ifstream is(p, std::ios::binary);
        if (!is) throw std::runtime_error("Failed to open JSON: " + p.string());
        json j;
        is >> j;
        return j;
    }

    static void write_atomic_json_pretty(const fs::path& p, const json& j) {
        metais::write_atomic_string(p, j.dump(2) + "\n");
    }

    struct FormatInfo {
        std::string layout;                 // "grid" | "sparse"
        std::uint32_t attributeCount = 0;
        std::uint32_t metaAttributeCount = 6;
        std::optional<std::uint32_t> sparseEntryByteSize; // ONLY for sparse
    };

    static FormatInfo load_format(const fs::path& format_json) {
        FormatInfo f;
        f.layout = "grid"; // default if file missing

        if (!fs::exists(format_json))
            return f;

        json j = read_json_file(format_json);
        if (!j.is_object())
            return f;

        if (j.contains("attributeLayout"))
            f.layout = j["attributeLayout"].get<std::string>();

        f.attributeCount     = (std::uint32_t)j.value("attributeCount", 0);
        f.metaAttributeCount = (std::uint32_t)j.value("metaAttributeCount", 6);

        if (f.layout == "grid" && j.contains("sparseEntryByteSize")) {
            throw std::runtime_error("format.json: grid layout must not define sparseEntryByteSize");
        }

        if (f.layout == "sparse") {
            if (!j.contains("sparseEntryByteSize"))
                throw std::runtime_error("format.json: sparse layout missing sparseEntryByteSize");

            f.sparseEntryByteSize =
                (std::uint32_t)j["sparseEntryByteSize"].get<std::uint32_t>();
        }

        return f;
    }

    static std::uint64_t file_size_or_throw(const fs::path& p) {
        std::error_code ec;
        auto sz = fs::file_size(p, ec);
        if (ec) throw std::runtime_error("file_size failed: " + p.string() + ": " + ec.message());
        return (std::uint64_t)sz;
    }

    static std::uint64_t count_rows_from_grid_file(const fs::path& attrs_bin, std::uint32_t A) {
        if (A == 0) return 0;
        const std::uint64_t bytes = file_size_or_throw(attrs_bin);
        const std::uint64_t row_bytes = (std::uint64_t)A * 4ull;
        if (bytes % row_bytes != 0) {
            throw std::runtime_error("attributes.bin size not multiple of row_bytes: " + attrs_bin.string());
        }
        return bytes / row_bytes;
    }

    // Count non-missing cells M (streaming)
    static std::uint64_t count_nonmissing_cells_grid(const fs::path& attrs_bin, std::uint64_t N, std::uint32_t A) {
        std::ifstream is(attrs_bin, std::ios::binary);
        if (!is) throw std::runtime_error("open failed: " + attrs_bin.string());

        std::uint64_t M = 0;
        const std::uint64_t total = N * (std::uint64_t)A;

        for (std::uint64_t i = 0; i < total; ++i) {
            const std::int32_t v = metais::read_i32_le(is);
            if (v != metais::kMissingI32) ++M;
        }
        return M;
    }

    static void grid_to_sparse_rewrite(
        const fs::path& attrs_bin_grid,
        const fs::path& attrs_bin_sparse_out,
        const fs::path& offsets_out,
        std::uint64_t N,
        std::uint32_t A
    ) {
        // Sparse: (AttrIndex U16, DictIndex U32) for each present cell.
        // Offsets: U32 byte offsets into sparse file, size N+1.

        std::ifstream is(attrs_bin_grid, std::ios::binary);
        if (!is) throw std::runtime_error("open failed: " + attrs_bin_grid.string());

        fs::create_directories(attrs_bin_sparse_out.parent_path());

        fs::path sparse_tmp = attrs_bin_sparse_out; sparse_tmp += ".tmp";

        std::ofstream os(sparse_tmp, std::ios::binary);
        if (!os) throw std::runtime_error("open failed: " + sparse_tmp.string());

        std::vector<std::uint32_t> offsets;
        offsets.resize((std::size_t)N + 1);

        std::uint64_t cur = 0; // bytes written so far
        offsets[0] = 0;

        for (std::uint64_t i = 0; i < N; ++i) {
            // row i
            for (std::uint32_t k = 0; k < A; ++k) {
                const std::int32_t v = metais::read_i32_le(is);
                if (v == metais::kMissingI32) continue;

                // attrIndex = k, valueIndex = v (stored as i32 but represents dict index)
                // Write U16 then U32
                metais::write_u16_le(os, (metais::AttrIndex)k);
                metais::write_u32_le(os, (metais::WireU32)(std::uint32_t)v);

                cur += 6;
                if (cur > 0xFFFFFFFFull) {
                    throw std::runtime_error("sparse attributes.bin exceeded 4GiB; need U64 offsets upgrade");
                }
            }
            offsets[(std::size_t)i + 1] = (std::uint32_t)cur;
        }

        if (!is) throw std::runtime_error("read failed while converting: " + attrs_bin_grid.string());
        if (!os) throw std::runtime_error("write failed while converting: " + sparse_tmp.string());

        os.close();

        // Write offsets atomically to final destination (helper makes its own .tmp)
        metais::write_atomic_u32le_file(offsets_out, offsets);

        // Atomic replace attributes file
        metais::atomic_rename(sparse_tmp, attrs_bin_sparse_out);
    }

    static void maybe_convert_one_type(const fs::path& type_dir, const std::string& kind_label) {
        const fs::path format_json = type_dir / "format.json";
        const fs::path attrs_bin   = type_dir / "attributes.bin";
        const fs::path offsets_bin = type_dir / "attribute_offsets.bin";

        if (!fs::exists(format_json)) return;
        if (!fs::exists(attrs_bin))   return; // no attrs file => nothing to convert

        FormatInfo fmt = load_format(format_json);

        // Only consider converting grid -> sparse
        if (fmt.layout != "grid") return;
        if (fmt.attributeCount == 0) return;

        const std::uint32_t A = fmt.attributeCount;
        const std::uint64_t N = count_rows_from_grid_file(attrs_bin, A);

        // Compute M and size comparison
        const std::uint64_t M = count_nonmissing_cells_grid(attrs_bin, N, A);

        const std::uint64_t grid_bytes  = N * (std::uint64_t)A * 4ull;
        const std::uint64_t sparse_bytes = 6ull * M + 4ull * ((std::uint64_t)N + 1ull);

        // If sparse doesn’t win, keep grid (and also remove stale offsets if any)
        if (sparse_bytes >= grid_bytes) {
            if (fs::exists(offsets_bin)) {
                std::error_code ec;
                fs::remove(offsets_bin, ec); // non-fatal
            }
            return;
        }

        // Convert
        std::cerr << "[pass3:sparse] Converting " << kind_label
                << " dir=" << type_dir.filename().string()
                << " N=" << N << " A=" << A
                << " grid=" << grid_bytes
                << " sparse=" << sparse_bytes
                << " (M=" << M << ")\n";

        // Write sparse into a temp name first, then atomically replace attributes.bin
        const fs::path sparse_target = type_dir / "attributes.bin"; // replace in-place
        const fs::path sparse_tmp_target = type_dir / "attributes.bin.sparse"; // actual path used for temp rename target

        // We’ll write into "attributes.bin.sparse" then rename over "attributes.bin"
        grid_to_sparse_rewrite(attrs_bin, sparse_tmp_target, offsets_bin, N, A);

        // Now atomically replace attributes.bin with sparse_tmp_target
        {
            fs::path tmp = sparse_tmp_target; // already finalized, but we still want atomic replace into final name
            // Rename sparse_tmp_target -> attributes.bin (replace)
            metais::atomic_rename(tmp, sparse_target);
        }

        // Update format.json
        json j = read_json_file(format_json);
        j["attributeLayout"] = "sparse";
        j["attributeCount"]  = A;
        j["sparseEntryByteSize"]  = 6;
        if (!j.contains("metaAttributeCount")) j["metaAttributeCount"] = 6;

        write_atomic_json_pretty(format_json, j);
    }

}

namespace metais {

    void pass3_optimize_attributes(const DirectoryLayout& layout) {
        const fs::path nodes_root = layout.nodes_packed;
        const fs::path rels_root  = layout.rels_packed;

        // Nodes: root/nodes/<citype>/
        if (fs::exists(nodes_root)) {
            for (const auto& ent : fs::directory_iterator(nodes_root)) {
                if (!ent.is_directory()) continue;
                const fs::path dir = ent.path();

                // Skip nodes_root/metaAttributes.json etc.
                if (dir.filename() == "metaAttributes.json") continue;

                // Only citype dirs have format.json
                if (!fs::exists(dir / "format.json")) continue;

                maybe_convert_one_type(dir, "node");
            }
        }

        // Relations: root/relations/<reltype>/
        if (fs::exists(rels_root)) {
            for (const auto& ent : fs::directory_iterator(rels_root)) {
                if (!ent.is_directory()) continue;
                const fs::path dir = ent.path();

                // Skip rels_root/metaAttributes.json etc.
                if (dir.filename() == "metaAttributes.json") continue;

                if (!fs::exists(dir / "format.json")) continue;

                maybe_convert_one_type(dir, "rel");
            }
        }

        std::cerr << "[pass3:sparse] done\n";
    }

}