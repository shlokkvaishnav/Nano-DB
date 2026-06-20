#pragma once
#include <vector>
#include <cstdint>
#include <string>
#include <cstring>
#include <stdexcept>
#include <fcntl.h>
#include <unistd.h>

namespace nanodb {
namespace raft {

struct PersistedEntry {
    uint64_t term;
    std::string command;
};

// The replicated log. Indices are 1-based (0 means "no entry", the
// sentinel for an empty log). NOT internally thread-safe -- same
// convention as PersistentState: the caller (RaftNode, already holding
// its own mutex) is responsible for synchronization.
//
// Entries can be truncated (a follower's conflicting suffix gets rolled
// back), so this can't be a pure append-only file. The whole file gets
// rewritten on every mutation instead: log entries here are rare (control
// plane membership changes), not a high-throughput path, so simple-and-
// correct wins over an append-only-with-truncation-markers scheme. Log
// compaction is out of scope until Phase 5.
class RaftLog {
public:
    void open_file(const std::string& path) {
        path_ = path;
        entries_.clear();
        int fd = ::open(path_.c_str(), O_RDONLY);
        if (fd < 0) { persist(); return; }
        std::string data;
        char buf[4096];
        ssize_t n;
        while ((n = ::read(fd, buf, sizeof(buf))) > 0) data.append(buf, n);
        ::close(fd);

        size_t pos = 0;
        while (pos < data.size()) {
            if (pos + sizeof(uint64_t) + sizeof(uint32_t) > data.size()) break;
            uint64_t term;
            std::memcpy(&term, data.data() + pos, sizeof(uint64_t));
            pos += sizeof(uint64_t);
            uint32_t len;
            std::memcpy(&len, data.data() + pos, sizeof(uint32_t));
            pos += sizeof(uint32_t);
            if (pos + len > data.size()) break;
            entries_.push_back({term, data.substr(pos, len)});
            pos += len;
        }
    }

    uint64_t last_index() const { return entries_.size(); }
    uint64_t last_term() const { return entries_.empty() ? 0 : entries_.back().term; }
    uint64_t term_at(uint64_t index) const {
        if (index == 0 || index > entries_.size()) return 0;
        return entries_[index - 1].term;
    }
    bool has_entry_at(uint64_t index) const { return index >= 1 && index <= entries_.size(); }
    std::string command_at(uint64_t index) const {
        if (index < 1 || index > entries_.size()) return "";
        return entries_[index - 1].command;
    }

    void truncate_and_append(uint64_t keep_count, const std::vector<PersistedEntry>& new_entries) {
        if (keep_count < entries_.size()) entries_.resize(keep_count);
        for (auto& e : new_entries) entries_.push_back(e);
        persist();
    }

    void append(uint64_t term, const std::string& command) {
        entries_.push_back({term, command});
        persist();
    }

private:
    std::string path_;
    std::vector<PersistedEntry> entries_;

    void persist() {
        int fd = ::open(path_.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd < 0) throw std::runtime_error("cannot open raft log file: " + path_);
        for (auto& e : entries_) {
            uint64_t term = e.term;
            uint32_t len = (uint32_t)e.command.size();
            if (::write(fd, &term, sizeof(term)) != (ssize_t)sizeof(term) ||
                ::write(fd, &len, sizeof(len)) != (ssize_t)sizeof(len) ||
                ::write(fd, e.command.data(), len) != (ssize_t)len) {
                ::close(fd);
                throw std::runtime_error("short write persisting raft log: " + path_);
            }
        }
        ::fsync(fd);
        ::close(fd);
    }
};

} // namespace raft
} // namespace nanodb
