#include "pass3_finalize_relations.h"

#include "binary_formats.h"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <vector>
#include <unordered_map>
#include <memory>
#include <cstdint>
#include <string>
#include <sstream>
#include <algorithm>
#include <system_error>

#include <nlohmann/json.hpp>

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace {

    static std::string read_file_text(const fs::path& p) {
        std::ifstream f(p, std::ios::binary);
        if (!f) throw std::runtime_error("Failed to open: " + p.string());
        std::ostringstream ss;
        ss << f.rdbuf();
        return ss.str();
    }

    static json read_json(const fs::path& p) {
        return json::parse(read_file_text(p));
    }

    // resolver.bin row: U16 citype_index, U32 local_index (6 bytes)
    static std::vector<metais::CitypeIndex> load_citype_of_gid(const fs::path& resolver_bin) {
        std::ifstream f(resolver_bin, std::ios::binary);
        if (!f) throw std::runtime_error("Failed to open resolver: " + resolver_bin.string());

        f.seekg(0, std::ios::end);
        const std::uint64_t sz = (std::uint64_t)f.tellg();
        f.seekg(0, std::ios::beg);

        // resolver row = u16_le + u32_le
        static constexpr std::size_t kResolverRowBytes = sizeof(metais::WireU16) + sizeof(metais::WireU32);
        if (sz % kResolverRowBytes != 0) {
            throw std::runtime_error("resolver.bin size not multiple of " + std::to_string(kResolverRowBytes) + ": " + std::to_string(sz));
        }

        const std::size_t N = (std::size_t)(sz / kResolverRowBytes);
        std::vector<metais::CitypeIndex> citype_of_gid(N);

        for (std::size_t i = 0; i < N; i++) {
            const metais::CitypeIndex ci = (metais::CitypeIndex)metais::read_u16_le(f);
            (void)metais::read_u32_le(f); // local index, unused here
            if (!f) throw std::runtime_error("Failed reading resolver.bin at row " + std::to_string(i));
            citype_of_gid[i] = ci;
        }
        return citype_of_gid;
    }

    static std::vector<std::string> load_citypes_list(const fs::path& citypes_json) {
        json j = read_json(citypes_json);
        if (!j.is_array()) throw std::runtime_error("citypes.json is not an array: " + citypes_json.string());
        std::vector<std::string> out;
        out.reserve(j.size());
        for (auto& v : j) out.push_back(v.get<std::string>());
        return out;
    }

    static std::string load_attribute_layout_for_reltype(const fs::path& reltype_format_json) {
        if (!fs::exists(reltype_format_json)) return "grid";
        json j = read_json(reltype_format_json);
        if (!j.is_object()) return "grid";
        if (!j.contains("attributeLayout")) return "grid";
        return j["attributeLayout"].get<std::string>();
    }

    // Key for (src_ci, tgt_ci), both U16
    static std::uint32_t pair_key(std::uint16_t a, std::uint16_t b) {
        return (std::uint32_t(a) << 16) | std::uint32_t(b);
    }
    static std::uint16_t key_a(std::uint32_t k) { return (std::uint16_t)(k >> 16); }
    static std::uint16_t key_b(std::uint32_t k) { return (std::uint16_t)(k & 0xFFFFu); }

}

namespace metais {

    void pass3_finalize_relation_edges(const DirectoryLayout& layout) {
        const fs::path rels_root = layout.rels_packed; // .../packed/relations
        const fs::path uuid_root = layout.uuids_dir;   // .../packed/uuids

        const fs::path resolver_bin = uuid_root / "resolver.bin";
        const fs::path citypes_json = uuid_root / "citypes.json";

        if (!fs::exists(rels_root)) {
            std::cerr << "[pass3] No relations dir: " << rels_root << "\n";
            return;
        }
        if (!fs::exists(resolver_bin) || !fs::exists(citypes_json)) {
            throw std::runtime_error("Pass 3 requires uuids/resolver.bin and uuids/citypes.json");
        }

        const std::vector<metais::CitypeIndex> citype_of_gid = load_citype_of_gid(resolver_bin);
        const std::vector<std::string> citypes = load_citypes_list(citypes_json);

        for (const auto& rel_dir_ent : fs::directory_iterator(rels_root)) {
            if (!rel_dir_ent.is_directory()) continue;

            const fs::path rel_dir = rel_dir_ent.path();              // .../relations/<RELTYPE>/
            const std::string reltype = rel_dir.filename().string();

            const fs::path tmp_edges = rel_dir / "tmp.edges.bin";
            if (!fs::exists(tmp_edges)) continue;

            const fs::path edges_root = rel_dir / "edges";

            const std::string attribute_layout =
                load_attribute_layout_for_reltype(rel_dir / "format.json");

            // temp partition area for this reltype
            const fs::path tmp_bucket_root = edges_root / ".pass3_tmp_buckets";
            if (fs::exists(tmp_bucket_root)) {
                std::error_code ec;
                fs::remove_all(tmp_bucket_root, ec);
            }
            fs::create_directories(tmp_bucket_root);

            // Stream tmp.edges.bin and append triples into per-(SRC,TGT) bucket files
            std::ifstream in(tmp_edges, std::ios::binary);
            if (!in) throw std::runtime_error("Failed to open: " + tmp_edges.string());

            in.seekg(0, std::ios::end);
            const std::uint64_t sz = (std::uint64_t)in.tellg();
            in.seekg(0, std::ios::beg);

            if (sz % metais::kEdgePairBytes != 0) {
                throw std::runtime_error(
                    "tmp.edges.bin size not multiple of " + std::to_string(metais::kEdgePairBytes) +
                    ": " + tmp_edges.string()
                );
            }
            const std::uint64_t n_edges = sz / metais::kEdgePairBytes;

            std::unordered_map<std::uint32_t, std::unique_ptr<std::ofstream>> bucket_out;

            auto get_bucket_stream = [&](std::uint32_t key) -> std::ofstream& {
                auto it = bucket_out.find(key);
                if (it != bucket_out.end()) return *it->second;

                const std::uint16_t a = key_a(key);
                const std::uint16_t b = key_b(key);

                std::string an = (a < citypes.size() ? citypes[a] : ("CI_" + std::to_string(a)));
                std::string bn = (b < citypes.size() ? citypes[b] : ("CI_" + std::to_string(b)));

                const fs::path bucket_file = tmp_bucket_root / (an + "__" + bn + ".triples.bin");
                auto os = std::make_unique<std::ofstream>(bucket_file, std::ios::binary | std::ios::app);
                if (!*os) throw std::runtime_error("Failed to open bucket: " + bucket_file.string());

                auto& ref = *os;
                bucket_out.emplace(key, std::move(os));
                return ref;
            };

            for (std::uint64_t relid64 = 0; relid64 < n_edges; relid64++) {
                const metais::EdgePair e = metais::read_edgepair_le(in);
                const metais::GlobalId src = e.src;
                const metais::GlobalId tgt = e.tgt;

                if (src >= citype_of_gid.size() || tgt >= citype_of_gid.size()) {
                    throw std::runtime_error(
                        "Edge global id out of range at relid " + std::to_string(relid64) +
                        " (src=" + std::to_string(src) + ", tgt=" + std::to_string(tgt) + ")"
                    );
                }

                const std::uint16_t src_ci = citype_of_gid[src];
                const std::uint16_t tgt_ci = citype_of_gid[tgt];
                const std::uint32_t key = pair_key(src_ci, tgt_ci);

                std::ofstream& bout = get_bucket_stream(key);

                metais::write_edgetriple_le(
                    bout,
                    metais::EdgeTriple{ src, tgt, (metais::LocalIndex)relid64 }
                );

                if (!bout) throw std::runtime_error("Write failed to bucket for reltype " + reltype);
            }

            // Close bucket outputs
            bucket_out.clear();

            // Finalize each bucket into edges/<SRC>__<TGT>/*
            for (const auto& bucket_ent : fs::directory_iterator(tmp_bucket_root)) {
                if (!bucket_ent.is_regular_file()) continue;

                const fs::path bucket_path = bucket_ent.path();
                const std::string fname = bucket_path.filename().string();
                const std::string suffix = ".triples.bin";
                if (fname.size() <= suffix.size() || fname.substr(fname.size() - suffix.size()) != suffix) continue;

                const std::string pair_name = fname.substr(0, fname.size() - suffix.size()); // <SRC>__<TGT>

                std::ifstream bf(bucket_path, std::ios::binary);
                if (!bf) throw std::runtime_error("Failed to open bucket: " + bucket_path.string());

                bf.seekg(0, std::ios::end);
                const std::uint64_t bsz = (std::uint64_t)bf.tellg();
                bf.seekg(0, std::ios::beg);

                if (bsz % metais::kEdgeTripleBytes != 0) {
                    throw std::runtime_error("Bucket size not multiple of 12: " + bucket_path.string());
                }

                const std::size_t n = (std::size_t)(bsz / metais::kEdgeTripleBytes);

                std::vector<metais::EdgeTriple> triples;
                triples.reserve(n);

                for (std::size_t i = 0; i < n; i++) {
                    triples.push_back(metais::read_edgetriple_le(bf));
                }

                std::sort(triples.begin(), triples.end(),
                    [](const metais::EdgeTriple& x, const metais::EdgeTriple& y) {
                        if (x.src != y.src) return x.src < y.src;
                        return x.tgt < y.tgt;
                    }
                );

                const fs::path out_dir = edges_root / pair_name;

                // src.tgt.bin + src.tgt.relid.bin
                std::vector<metais::EdgePair> st;
                st.reserve(n);

                std::vector<metais::LocalIndex> st_relid;
                st_relid.reserve(n);

                for (const auto& t : triples) {
                    st.push_back(metais::EdgePair{ t.src, t.tgt });
                    st_relid.push_back(t.relid);
                }

                metais::write_atomic_edgepairs_file(out_dir / "src.tgt.bin", st);
                metais::write_atomic_localindex_le_file(out_dir / "src.tgt.relid.bin", st_relid);

                // tgt.src.bin + tgt.src.relid.bin
                std::vector<metais::EdgeTriple> swapped;
                swapped.reserve(n);
                for (const auto& t : triples) swapped.push_back(metais::EdgeTriple{ t.tgt, t.src, t.relid });

                std::sort(swapped.begin(), swapped.end(),
                    [](const metais::EdgeTriple& x, const metais::EdgeTriple& y) {
                        if (x.src != y.src) return x.src < y.src;
                        return x.tgt < y.tgt;
                    }
                );

                std::vector<metais::EdgePair> ts;
                ts.reserve(n);

                std::vector<metais::LocalIndex> ts_relid;
                ts_relid.reserve(n);

                for (const auto& t : swapped) {
                    ts.push_back(metais::EdgePair{ t.src, t.tgt });
                    ts_relid.push_back(t.relid);
                }

                metais::write_atomic_edgepairs_file(out_dir / "tgt.src.bin", ts);
                metais::write_atomic_localindex_le_file(out_dir / "tgt.src.relid.bin", ts_relid);

                // meta.json
                std::string src_type = pair_name;
                std::string tgt_type = "";
                const std::size_t pos = pair_name.find("__");
                if (pos != std::string::npos) {
                    src_type = pair_name.substr(0, pos);
                    tgt_type = pair_name.substr(pos + 2);
                }

                json meta = {
                    {"reltype", reltype},
                    {"sourceType", src_type},
                    {"targetType", tgt_type},
                    {"relationCount", (std::uint64_t)n},
                    {"attributeLayout", attribute_layout},
                };
                metais::write_atomic_string(out_dir / "meta.json", meta.dump(2) + "\n");
            }

            // Remove temp buckets (non-fatal if it fails)
            {
                std::error_code ec;
                fs::remove_all(tmp_bucket_root, ec);
                if (ec) {
                    std::cerr << "[pass3] Warning: failed to remove temp bucket dir: "
                            << tmp_bucket_root << " : " << ec.message() << "\n";
                }
            }

            // Delete tmp.edges.bin ONLY after successful finalization
            {
                std::error_code ec;
                fs::remove(tmp_edges, ec);
                if (ec) throw std::runtime_error("Failed to delete tmp.edges.bin: " + tmp_edges.string() + " : " + ec.message());
            }

            std::cerr << "[pass3] Finalized edges for reltype " << reltype
                    << " from " << n_edges << " relations.\n";
        }
    }

}