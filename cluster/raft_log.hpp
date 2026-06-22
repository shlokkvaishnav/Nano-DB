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
// correct wins over an append-only-with-truncation-markers scheme.
//
// --- Log Compaction (Phase 7) ---
//
// Supports Raft snapshotting (paper §7). After compact(N), entries at
// absolute indices [1..N] are replaced by a snapshot: only
// snapshot_last_index_ (N), snapshot_last_term_ (term of entry N), and
// snapshot_data_ (the state machine state at N) are retained. entries_
// then contains only indices [N+1..last_index()].
//
// All index-space methods (term_at, command_at, has_entry_at,
// last_index) remain in ABSOLUTE terms after compaction, so callers
// (RaftNode) need no adjustment.
//
// File format (Phase 7, detected by the 4-byte magic 0x4E414654):
//   uint32_t magic              = 0x4E414654 ('N','A','F','T')
//   uint64_t snapshot_last_idx
//   uint64_t snapshot_last_trm
//   uint32_t snapshot_data_len
//   [snapshot_data_len bytes]
//   -- then per-entry: --
//   uint64_t term
//   uint32_t cmd_len
//   [cmd_len bytes]
//
// Old format (no magic, written by Phases 3–6): no snapshot header,
// just the per-entry records. Detected on open_file by the absence of
// the magic bytes; migrated to the new format on the next persist().
class RaftLog {
public:
    static constexpr uint32_t kMagic = 0x4E414654u;

    void open_file(const std::string& path) {
        path_ = path;
        entries_.clear();
        snapshot_last_index_ = 0;
        snapshot_last_term_  = 0;
        snapshot_data_.clear();

        int fd = ::open(path_.c_str(), O_RDONLY);
        if (fd < 0) { persist(); return; }  // new file

        std::string data;
        char buf[4096];
        ssize_t n;
        while ((n = ::read(fd, buf, sizeof(buf))) > 0) data.append(buf, n);
        ::close(fd);

        size_t pos = 0;
        if (data.size() >= sizeof(uint32_t)) {
            uint32_t maybe_magic;
            std::memcpy(&maybe_magic, data.data(), sizeof(uint32_t));
            if (maybe_magic == kMagic) {
                // New format: read snapshot header first.
                pos += sizeof(uint32_t);
                if (pos + sizeof(uint64_t) * 2 + sizeof(uint32_t) > data.size()) {
                    persist(); return;  // truncated header -- start fresh
                }
                std::memcpy(&snapshot_last_index_, data.data() + pos, sizeof(uint64_t));
                pos += sizeof(uint64_t);
                std::memcpy(&snapshot_last_term_,  data.data() + pos, sizeof(uint64_t));
                pos += sizeof(uint64_t);
                uint32_t snap_len;
                std::memcpy(&snap_len, data.data() + pos, sizeof(uint32_t));
                pos += sizeof(uint32_t);
                if (pos + snap_len > data.size()) { persist(); return; }
                snapshot_data_ = data.substr(pos, snap_len);
                pos += snap_len;
            }
            // else: old format, pos stays at 0, no snapshot header.
        }

        // Read per-entry records (same binary layout in both formats).
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

    // --- Accessors (all indices ABSOLUTE) ---

    // Highest log index present, including the snapshot coverage.
    uint64_t last_index() const {
        return snapshot_last_index_ + entries_.size();
    }

    // Term of the highest log index. Returns snapshot_last_term_ if
    // entries_ is empty (everything was compacted) or 0 for a truly
    // empty log (no snapshot, no entries).
    uint64_t last_term() const {
        if (!entries_.empty()) return entries_.back().term;
        return snapshot_last_term_;  // 0 if no snapshot yet
    }

    // Term at absolute index i. Returns snapshot_last_term_ for any
    // index within [1..snapshot_last_index_] (the snapshot covers those
    // entries; we only store the term of the last one). Returns 0 for
    // i==0 or i>last_index().
    uint64_t term_at(uint64_t index) const {
        if (index == 0) return 0;
        if (index > last_index()) return 0;
        if (index <= snapshot_last_index_) return snapshot_last_term_;
        return entries_[index - 1 - snapshot_last_index_].term;
    }

    // True if index is in [1..last_index()], i.e. covered either by
    // the snapshot or by a real entry.
    bool has_entry_at(uint64_t index) const {
        return index >= 1 && index <= last_index();
    }

    // True if we have the actual persisted entry (not just snapshot
    // coverage). Used by AppendEntries consistency checks, which can
    // only verify terms of real entries.
    bool has_real_entry_at(uint64_t index) const {
        return index > snapshot_last_index_ && index <= last_index();
    }

    // Command string at absolute index i. Returns "" for i within the
    // snapshot range or out of bounds (those entries are gone).
    std::string command_at(uint64_t index) const {
        if (index < 1 || index <= snapshot_last_index_ || index > last_index()) return "";
        return entries_[index - 1 - snapshot_last_index_].command;
    }

    // Snapshot accessors.
    uint64_t    snapshot_last_index() const { return snapshot_last_index_; }
    uint64_t    snapshot_last_term()  const { return snapshot_last_term_;  }
    const std::string& snapshot_data() const { return snapshot_data_; }

    // --- Mutations ---

    // keep_count is ABSOLUTE: truncate so only keep_count entries
    // remain, then append new_entries. new_entries[j] has absolute
    // index keep_count+1+j; entries within the snapshot range are
    // silently skipped (they're already applied).
    void truncate_and_append(uint64_t keep_count,
                             const std::vector<PersistedEntry>& new_entries) {
        uint64_t physical_keep = (keep_count > snapshot_last_index_)
            ? (keep_count - snapshot_last_index_) : 0;
        if (physical_keep < entries_.size()) entries_.resize(physical_keep);

        for (size_t j = 0; j < new_entries.size(); j++) {
            uint64_t abs_idx = keep_count + 1 + j;
            if (abs_idx <= snapshot_last_index_) continue;
            entries_.push_back(new_entries[j]);
        }
        persist();
    }

    void append(uint64_t term, const std::string& command) {
        entries_.push_back({term, command});
        persist();
    }

    // Compact entries [1..up_to_index] into a snapshot. up_to_index must
    // satisfy snapshot_last_index_ < up_to_index. The upper bound check
    // (up_to_index <= last_index()) is intentionally absent here: for
    // leader-initiated compaction RaftNode::compact() already guards
    // up_to_index <= last_applied_ <= last_index(). For InstallSnapshot,
    // the leader sends a snapshot that covers entries the follower never
    // received (up_to_index > last_index()), and this function must still
    // install it -- that's the whole point. The remove_count clamp below
    // handles both cases safely.
    void compact(uint64_t up_to_index, uint64_t snapshot_term,
                 const std::string& snapshot_data) {
        if (up_to_index <= snapshot_last_index_) return;
        uint64_t remove_count = up_to_index - snapshot_last_index_;
        if (remove_count > entries_.size()) remove_count = entries_.size();
        entries_.erase(entries_.begin(),
                       entries_.begin() + static_cast<ptrdiff_t>(remove_count));
        snapshot_last_index_ = up_to_index;
        snapshot_last_term_  = snapshot_term;
        snapshot_data_       = snapshot_data;
        persist();
    }

private:
    std::string path_;
    std::vector<PersistedEntry> entries_;

    uint64_t    snapshot_last_index_ = 0;
    uint64_t    snapshot_last_term_  = 0;
    std::string snapshot_data_;

    void persist() {
        int fd = ::open(path_.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd < 0) throw std::runtime_error("cannot open raft log file: " + path_);

        // Header.
        uint32_t magic    = kMagic;
        uint64_t snap_idx = snapshot_last_index_;
        uint64_t snap_trm = snapshot_last_term_;
        uint32_t snap_len = static_cast<uint32_t>(snapshot_data_.size());

        auto write_exact = [&](const void* p, size_t n) {
            if (::write(fd, p, n) != static_cast<ssize_t>(n)) {
                ::close(fd);
                throw std::runtime_error("short write persisting raft log: " + path_);
            }
        };
        write_exact(&magic,    sizeof(magic));
        write_exact(&snap_idx, sizeof(snap_idx));
        write_exact(&snap_trm, sizeof(snap_trm));
        write_exact(&snap_len, sizeof(snap_len));
        if (snap_len > 0) write_exact(snapshot_data_.data(), snap_len);

        // Entries.
        for (auto& e : entries_) {
            uint64_t term = e.term;
            uint32_t len  = static_cast<uint32_t>(e.command.size());
            write_exact(&term, sizeof(term));
            write_exact(&len,  sizeof(len));
            write_exact(e.command.data(), len);
        }
        ::fsync(fd);
        ::close(fd);
    }
};

} // namespace raft
} // namespace nanodb
