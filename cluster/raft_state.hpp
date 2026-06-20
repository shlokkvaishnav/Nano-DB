#pragma once
#include <string>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <fcntl.h>
#include <unistd.h>

namespace nanodb {
namespace raft {

// Durable storage for currentTerm and votedFor -- the two pieces of Raft
// state that MUST be on disk before a node responds to any RequestVote or
// AppendEntries RPC. Losing these on crash-restart can cause double-voting
// in the same term, which breaks Raft's election safety guarantee. Uses
// raw POSIX I/O specifically so fsync() is unambiguous; std::ofstream's
// flush() only pushes to the OS, not to physical disk.
class PersistentState {
public:
    void open_file(const std::string& path) {
        path_ = path;
        int fd = ::open(path_.c_str(), O_RDONLY);
        if (fd >= 0) {
            char buf[sizeof(uint64_t) + sizeof(int32_t)];
            ssize_t n = ::read(fd, buf, sizeof(buf));
            ::close(fd);
            if (n == (ssize_t)sizeof(buf)) {
                std::memcpy(&current_term_, buf, sizeof(uint64_t));
                std::memcpy(&voted_for_, buf + sizeof(uint64_t), sizeof(int32_t));
                return;
            }
        }
        persist(); // first run, or corrupt/short file: write fresh defaults
    }

    uint64_t current_term() const { return current_term_; }
    int32_t voted_for() const { return voted_for_; }

    void set(uint64_t term, int32_t voted_for) {
        current_term_ = term;
        voted_for_ = voted_for;
        persist();
    }

private:
    std::string path_;
    uint64_t current_term_ = 0;
    int32_t voted_for_ = -1;

    void persist() {
        int fd = ::open(path_.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd < 0) throw std::runtime_error("cannot open raft state file: " + path_);
        char buf[sizeof(uint64_t) + sizeof(int32_t)];
        std::memcpy(buf, &current_term_, sizeof(uint64_t));
        std::memcpy(buf + sizeof(uint64_t), &voted_for_, sizeof(int32_t));
        ssize_t written = ::write(fd, buf, sizeof(buf));
        if (written != (ssize_t)sizeof(buf)) {
            ::close(fd);
            throw std::runtime_error("short write persisting raft state: " + path_);
        }
        ::fsync(fd);
        ::close(fd);
    }
};

} // namespace raft
} // namespace nanodb
