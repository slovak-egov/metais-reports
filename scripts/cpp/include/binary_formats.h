#pragma once

#include <cstdint>
#include <cstring>
#include <istream>
#include <ostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <array>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <system_error>

namespace metais {

    namespace fs = std::filesystem;

    inline void atomic_rename(const fs::path& tmp, const fs::path& final) {
        std::error_code ec;
        fs::rename(tmp, final, ec);
        if (!ec) return;
        fs::remove(final, ec);
        ec.clear();
        fs::rename(tmp, final, ec);
        if (ec) throw std::runtime_error("rename failed: " + final.string() + ": " + ec.message());
    }

    inline void write_atomic_bytes(const fs::path& path, const void* data, std::size_t n) {
        fs::create_directories(path.parent_path());
        fs::path tmp = path; tmp += ".tmp";
        {
            std::ofstream os(tmp, std::ios::binary);
            if (!os) throw std::runtime_error("open failed: " + tmp.string());
            os.write(reinterpret_cast<const char*>(data), (std::streamsize)n);
            if (!os) throw std::runtime_error("write failed: " + tmp.string());
        }
        atomic_rename(tmp, path);
    }

    inline void write_atomic_string(const fs::path& path, const std::string& s) {
        write_atomic_bytes(path, s.data(), s.size());
    }

    // -------------------------
    // Semantic aliases
    // -------------------------
    using DictIndex   = std::uint32_t;
    using GlobalId    = std::uint32_t;
    using LocalIndex  = std::uint32_t;
    using CitypeIndex = std::uint16_t; // up to 65535 different citypes
    using AttrIndex   = std::uint16_t; // for dense layout (U16 attrIndex), up to 65535 attributes

    static constexpr std::int32_t kMissingI32 = -1;

    // -------------------------
    // Little-endian IO helpers
    // -------------------------
    inline void write_u16_le(std::ostream& os, std::uint16_t v) {
        unsigned char b[2] = {
            static_cast<unsigned char>(v & 0xFFu),
            static_cast<unsigned char>((v >> 8) & 0xFFu),
        };
        os.write(reinterpret_cast<const char*>(b), 2);
        if (!os) throw std::runtime_error("write_u16_le failed");
    }

    inline void write_u32_le(std::ostream& os, std::uint32_t v) {
        unsigned char b[4] = {
            static_cast<unsigned char>(v & 0xFFu),
            static_cast<unsigned char>((v >> 8) & 0xFFu),
            static_cast<unsigned char>((v >> 16) & 0xFFu),
            static_cast<unsigned char>((v >> 24) & 0xFFu),
        };
        os.write(reinterpret_cast<const char*>(b), 4);
        if (!os) throw std::runtime_error("write_u32_le failed");
    }

    inline void write_i32_le(std::ostream& os, std::int32_t v) {
        write_u32_le(os, static_cast<std::uint32_t>(v));
    }

    inline void write_u64_le(std::ostream& os, std::uint64_t v) {
        unsigned char b[8] = {
            static_cast<unsigned char>(v & 0xFFull),
            static_cast<unsigned char>((v >> 8) & 0xFFull),
            static_cast<unsigned char>((v >> 16) & 0xFFull),
            static_cast<unsigned char>((v >> 24) & 0xFFull),
            static_cast<unsigned char>((v >> 32) & 0xFFull),
            static_cast<unsigned char>((v >> 40) & 0xFFull),
            static_cast<unsigned char>((v >> 48) & 0xFFull),
            static_cast<unsigned char>((v >> 56) & 0xFFull),
        };
        os.write(reinterpret_cast<const char*>(b), 8);
        if (!os) throw std::runtime_error("write_u64_le failed");
    }

    inline std::uint16_t read_u16_le(std::istream& is) {
        unsigned char b[2];
        is.read(reinterpret_cast<char*>(b), 2);
        if (!is) throw std::runtime_error("read_u16_le failed");
        return (std::uint16_t)b[0] | ((std::uint16_t)b[1] << 8);
    }

    inline std::uint32_t read_u32_le(std::istream& is) {
        unsigned char b[4];
        is.read(reinterpret_cast<char*>(b), 4);
        if (!is) throw std::runtime_error("read_u32_le failed");
        return (std::uint32_t)b[0]
            | ((std::uint32_t)b[1] << 8)
            | ((std::uint32_t)b[2] << 16)
            | ((std::uint32_t)b[3] << 24);
    }

    inline std::int32_t read_i32_le(std::istream& is) {
        return static_cast<std::int32_t>(read_u32_le(is));
    }

    inline std::uint64_t read_u64_le(std::istream& is) {
        unsigned char b[8];
        is.read(reinterpret_cast<char*>(b), 8);
        if (!is) throw std::runtime_error("read_u64_le failed");
        return (std::uint64_t)b[0]
            | ((std::uint64_t)b[1] << 8)
            | ((std::uint64_t)b[2] << 16)
            | ((std::uint64_t)b[3] << 24)
            | ((std::uint64_t)b[4] << 32)
            | ((std::uint64_t)b[5] << 40)
            | ((std::uint64_t)b[6] << 48)
            | ((std::uint64_t)b[7] << 56);
    }

    inline void write_atomic_u32le_file(const fs::path& path, const std::vector<std::uint32_t>& v) {
        fs::create_directories(path.parent_path());
        fs::path tmp = path; tmp += ".tmp";
        {
            std::ofstream os(tmp, std::ios::binary);
            if (!os) throw std::runtime_error("open failed: " + tmp.string());
            for (std::uint32_t x : v) write_u32_le(os, x);
            if (!os) throw std::runtime_error("write failed: " + tmp.string());
        }
        atomic_rename(tmp, path);
    }

    inline void write_atomic_u64le_file(const fs::path& path, const std::vector<std::uint64_t>& v) {
        fs::create_directories(path.parent_path());
        fs::path tmp = path; tmp += ".tmp";
        {
            std::ofstream os(tmp, std::ios::binary);
            if (!os) throw std::runtime_error("open failed: " + tmp.string());
            for (std::uint64_t x : v) write_u64_le(os, x);
        }
        atomic_rename(tmp, path);
    }

    // -------------------------
    // UUID128
    // -------------------------
    // Comparison is lexicographic on raw UUID bytes.
    // We'll store hi as the first 8 bytes, lo as last 8 bytes, interpreted as big-endian within each.
    // This makes (hi,lo) comparison match byte-wise comparison.
    struct Uuid128 {
        std::uint64_t hi = 0;
        std::uint64_t lo = 0;

        friend bool operator==(const Uuid128& a, const Uuid128& b) {
            return a.hi == b.hi && a.lo == b.lo;
        }
        friend bool operator<(const Uuid128& a, const Uuid128& b) {
            return (a.hi < b.hi) || (a.hi == b.hi && a.lo < b.lo);
        }
    };

    inline std::uint64_t be64_from_8(const unsigned char* p) {
        return (std::uint64_t)p[0] << 56
            | (std::uint64_t)p[1] << 48
            | (std::uint64_t)p[2] << 40
            | (std::uint64_t)p[3] << 32
            | (std::uint64_t)p[4] << 24
            | (std::uint64_t)p[5] << 16
            | (std::uint64_t)p[6] << 8
            | (std::uint64_t)p[7];
    }

    inline void be64_to_8(std::uint64_t v, unsigned char* p) {
        p[0] = (unsigned char)((v >> 56) & 0xFF);
        p[1] = (unsigned char)((v >> 48) & 0xFF);
        p[2] = (unsigned char)((v >> 40) & 0xFF);
        p[3] = (unsigned char)((v >> 32) & 0xFF);
        p[4] = (unsigned char)((v >> 24) & 0xFF);
        p[5] = (unsigned char)((v >> 16) & 0xFF);
        p[6] = (unsigned char)((v >> 8) & 0xFF);
        p[7] = (unsigned char)(v & 0xFF);
    }

    inline bool is_hex(char c) {
        return std::isxdigit(static_cast<unsigned char>(c)) != 0;
    }

    inline int hexval(char c) {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return 10 + (c - 'a');
        if (c >= 'A' && c <= 'F') return 10 + (c - 'A');
        return -1;
    }

    // Parse UUID in either 36-char (with hyphens) or 32 hex.
    inline Uuid128 uuid_from_string(std::string_view s) {
        // Strip hyphens into 32 hex chars
        char hex[32];
        int n = 0;
        for (char c : s) {
            if (c == '-') continue;
            if (!is_hex(c)) throw std::runtime_error("uuid_from_string: non-hex char");
            if (n >= 32) throw std::runtime_error("uuid_from_string: too long");
            hex[n++] = c;
        }
        if (n != 32) throw std::runtime_error("uuid_from_string: wrong length");

        unsigned char bytes[16];
        for (int i = 0; i < 16; ++i) {
            int hi = hexval(hex[2*i]);
            int lo = hexval(hex[2*i + 1]);
            if (hi < 0 || lo < 0) throw std::runtime_error("uuid_from_string: bad hex");
            bytes[i] = (unsigned char)((hi << 4) | lo);
        }

        Uuid128 u;
        u.hi = be64_from_8(bytes);
        u.lo = be64_from_8(bytes + 8);
        return u;
    }

    inline std::string uuid_to_string(const Uuid128& u) {
        unsigned char b[16];
        be64_to_8(u.hi, b);
        be64_to_8(u.lo, b + 8);

        auto hex2 = [](unsigned char x) -> char {
            static const char* d = "0123456789abcdef";
            return d[x & 0xF];
        };

        char out[36];
        int p = 0;
        for (int i = 0; i < 16; ++i) {
            if (i == 4 || i == 6 || i == 8 || i == 10) out[p++] = '-';
            out[p++] = hex2((unsigned char)(b[i] >> 4));
            out[p++] = hex2((unsigned char)(b[i] & 0xF));
        }
        return std::string(out, out + 36);
    }

    // Write/read as raw 16 bytes (RFC hex byte order), not LE/BE integers.
    // That keeps files portable and comparisons sane.
    inline void write_uuid_raw16(std::ostream& os, const Uuid128& u) {
        unsigned char b[16];
        be64_to_8(u.hi, b);
        be64_to_8(u.lo, b + 8);
        os.write(reinterpret_cast<const char*>(b), 16);
        if (!os) throw std::runtime_error("write_uuid_raw16 failed");
    }

    inline void write_atomic_uuid16_file(const fs::path& path, const std::vector<Uuid128>& v) {
        fs::create_directories(path.parent_path());
        fs::path tmp = path; tmp += ".tmp";
        {
            std::ofstream os(tmp, std::ios::binary);
            if (!os) throw std::runtime_error("open failed: " + tmp.string());
            for (const auto& u : v) write_uuid_raw16(os, u);
            if (!os) throw std::runtime_error("write failed: " + tmp.string());
        }
        atomic_rename(tmp, path);
    }

    inline Uuid128 read_uuid_raw16(std::istream& is) {
        unsigned char b[16];
        is.read(reinterpret_cast<char*>(b), 16);
        if (!is) throw std::runtime_error("read_uuid_raw16 failed");
        Uuid128 u;
        u.hi = be64_from_8(b);
        u.lo = be64_from_8(b + 8);
        return u;
    }

}