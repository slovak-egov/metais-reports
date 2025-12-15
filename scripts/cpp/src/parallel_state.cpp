#include <fstream>
#include <stdexcept>
#include <sys/file.h>
#include <fcntl.h>
#include <unistd.h>

#include "../include/parallel_state.h"

namespace fs = std::filesystem;

namespace {

    void write_all(const fs::path& p, const std::string& s) {
        fs::path tmp = p;
        tmp += ".tmp";
        {
            std::ofstream out(tmp, std::ios::binary);
            if (!out) throw std::runtime_error("Cannot write: " + tmp.string());
            out << s;
        }
        fs::rename(tmp, p);
    }

    long read_long_default(const fs::path& p, long def) {
        std::ifstream in(p);
        if (!in) return def;
        long v;
        in >> v;
        return in ? v : def;
    }

    long claim_next_offset_locked(const fs::path& next_path, int page_size) {
        long cur = read_long_default(next_path, 0);
        long next = cur + page_size;
        write_all(next_path, std::to_string(next));
        return cur;
    }

    int open_lockfile(const fs::path& lock_path) {
        int fd = ::open(lock_path.c_str(), O_CREAT | O_RDWR, 0644);
        if (fd < 0) throw std::runtime_error("Cannot open lockfile: " + lock_path.string());
        return fd;
    }

    // helper: lock next_offset+failed_offsets using same lock file
    static int lock_state(const fs::path& state_dir) {
        fs::create_directories(state_dir);
        const fs::path lock_path = state_dir / "lock";
        int fd = ::open(lock_path.c_str(), O_CREAT | O_RDWR, 0644);
        if (fd < 0) throw std::runtime_error("Cannot open lockfile: " + lock_path.string());
        if (flock(fd, LOCK_EX) != 0) { close(fd); throw std::runtime_error("flock LOCK_EX failed"); }
        return fd;
    }
    static void unlock_state(int fd) {
        flock(fd, LOCK_UN);
        close(fd);
    }

}

namespace metais {

    long claim_next_offset(const fs::path& state_dir, int page_size) {
        fs::create_directories(state_dir);
        const fs::path lock_path = state_dir / "lock";
        const fs::path next_path = state_dir / "next_offset.txt";

        int fd = open_lockfile(lock_path);
        if (flock(fd, LOCK_EX) != 0) { close(fd); throw std::runtime_error("flock LOCK_EX failed"); }

        long off = claim_next_offset_locked(next_path, page_size);

        flock(fd, LOCK_UN);
        close(fd);
        return off;
    }

    void set_stop_at(const fs::path& state_dir, long stop_at) {
        fs::create_directories(state_dir);
        write_all(state_dir / "stop_at.txt", std::to_string(stop_at));
    }

    bool get_stop_at(const fs::path& state_dir, long& out_stop_at) {
        fs::path p = state_dir / "stop_at.txt";
        std::ifstream in(p);
        if (!in) return false;
        long v; in >> v;
        if (!in) return false;
        out_stop_at = v;
        return true;
    }

    void write_shared_token(const fs::path& state_dir, const std::string& token) {
        fs::create_directories(state_dir);
        write_all(state_dir / "token.txt", token);
    }

    std::string read_shared_token(const fs::path& state_dir) {
        fs::path p = state_dir / "token.txt";
        std::ifstream in(p);
        if (!in) return "";
        std::string tok;
        std::getline(in, tok);
        return tok;
    }

    long read_next_offset(const fs::path& state_dir, long def) {
        const fs::path next_path = state_dir / "next_offset.txt";
        return read_long_default(next_path, def);
    }

    void write_next_offset_if_smaller(const fs::path& state_dir, long candidate) {
        int fd = lock_state(state_dir);
        const fs::path next_path = state_dir / "next_offset.txt";
        long cur = read_long_default(next_path, 0);
        if (candidate < cur) {
            write_all(next_path, std::to_string(candidate));
        }
        unlock_state(fd);
    }

    void record_failed_offset(const fs::path& state_dir, long offset) {
        int fd = lock_state(state_dir);
        const fs::path p = state_dir / "failed_offsets.txt";
        std::ofstream out(p, std::ios::app);
        if (!out) { unlock_state(fd); throw std::runtime_error("Cannot append: " + p.string()); }
        out << offset << "\n";
        unlock_state(fd);
    }

    bool read_and_clear_min_failed_offset(const fs::path& state_dir, long& out_min) {
        int fd = lock_state(state_dir);
        const fs::path p = state_dir / "failed_offsets.txt";
        std::ifstream in(p);
        if (!in) { unlock_state(fd); return false; }

        long v;
        bool any = false;
        long mn = 0;
        while (in >> v) {
            if (!any) { mn = v; any = true; }
            else mn = std::min(mn, v);
        }

        // clear file
        {
            std::ofstream out(p, std::ios::trunc);
        }

        unlock_state(fd);
        if (!any) return false;
        out_min = mn;
        return true;
    }

}