#pragma once
#include <atomic>
#include <mutex>
#include <thread>
#include <chrono>
#include <random>
#include <future>
#include <vector>
#include <map>
#include <string>
#include <algorithm>
#include <functional>
#include <grpcpp/grpcpp.h>
#include "raft.grpc.pb.h"
#include "raft_state.hpp"
#include "raft_log.hpp"

namespace nanodb {
namespace raft {

enum class Role { Follower, Candidate, Leader };

struct RaftPeer {
    int node_id;
    std::string host;
    int port;
    std::string address() const { return host + ":" + std::to_string(port); }
};

// Pure decision logic for commit-index advancement, pulled out of RaftNode
// specifically so it can be unit tested with constructed scenarios rather
// than only being exercised incidentally by a live cluster's happy path.
// This is the single trickiest correctness rule in Raft (paper section
// 5.4.2, "Figure 8"): a leader may only directly commit an entry from its
// OWN current term, never an entry from an earlier term, even if that
// earlier entry is already on a majority of logs. Committing it happens
// only indirectly, as a side effect of a later current-term entry being
// committed. Returns current_commit_index unchanged if nothing new should
// commit yet.
inline uint64_t compute_new_commit_index(uint64_t leader_last_index,
                                          const std::map<int, uint64_t>& match_index,
                                          size_t cluster_size,
                                          uint64_t current_commit_index,
                                          uint64_t current_term,
                                          const std::function<uint64_t(uint64_t)>& term_at) {
    std::vector<uint64_t> match_values;
    match_values.push_back(leader_last_index);
    for (auto& [id, m] : match_index) match_values.push_back(m);
    std::sort(match_values.begin(), match_values.end(), std::greater<uint64_t>());
    int majority = (int)(cluster_size / 2) + 1;
    uint64_t candidate_n = match_values[majority - 1];

    if (candidate_n > current_commit_index && term_at(candidate_n) == current_term) {
        return candidate_n;
    }
    return current_commit_index;
}

class RaftNode {
public:
    RaftNode(int node_id, std::vector<RaftPeer> peers, const std::string& state_path,
             const std::string& log_path)
        : node_id_(node_id), peers_(std::move(peers)) {
        state_.open_file(state_path);
        log_.open_file(log_path);
        for (auto& p : peers_) {
            if (p.node_id == node_id_) continue;
            auto channel = grpc::CreateChannel(p.address(), grpc::InsecureChannelCredentials());
            stubs_[p.node_id] = RaftService::NewStub(channel);
        }
        reset_election_deadline();
    }

    void start() {
        running_ = true;
        ticker_thread_ = std::thread([this] { tick_loop(); });
    }
    void stop() {
        running_ = false;
        if (ticker_thread_.joinable()) ticker_thread_.join();
    }

    // Returns true if the command was replicated to a majority and
    // committed within timeout_ms. False if this node isn't the leader,
    // it stepped down while the entry was in flight, or the timeout
    // expired. The caller should NOT assume false means the command was
    // not applied anywhere -- it may still commit later via a new leader
    // if it was already replicated to a majority before a step-down.
    bool propose(const std::string& command, int timeout_ms = 2000) {
        uint64_t my_index, my_term;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (role_ != Role::Leader) return false;
            my_term = state_.current_term();
            log_.append(my_term, command);
            my_index = log_.last_index();
            // Without this, commit-index advancement only happens as a side
            // effect of a peer acking AppendEntries (in
            // send_append_entries_to_peer). For a cluster where the
            // leader's own log already constitutes a majority -- the
            // degenerate but valid single-node case, cluster_size=1 -- that
            // path is never reached at all (replicate_to_followers() has no
            // peers to iterate over), so nothing could ever commit. Safe to
            // call unconditionally: in a multi-node cluster this is just an
            // early check that's a no-op until real peer acks arrive, since
            // their match_index entries won't yet reflect this brand-new
            // entry.
            advance_commit_index_locked();
        }
        replicate_to_followers();

        auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (std::chrono::steady_clock::now() < deadline) {
            {
                std::lock_guard<std::mutex> lock(mutex_);
                if (role_ != Role::Leader || state_.current_term() != my_term) return false;
                if (commit_index_ >= my_index) return true;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        return false;
    }

    grpc::Status handle_request_vote(const RequestVoteRequest* req, RequestVoteResponse* resp) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (req->term() > state_.current_term()) become_follower_locked(req->term());
        resp->set_term(state_.current_term());
        if (req->term() < state_.current_term()) { resp->set_vote_granted(false); return grpc::Status::OK; }

        bool can_vote = (state_.voted_for() == -1 || state_.voted_for() == req->candidate_id());
        bool log_ok = (req->last_log_term() > log_.last_term()) ||
                      (req->last_log_term() == log_.last_term() && req->last_log_index() >= log_.last_index());
        if (can_vote && log_ok) {
            state_.set(state_.current_term(), req->candidate_id());
            reset_election_deadline_locked();
            resp->set_vote_granted(true);
        } else {
            resp->set_vote_granted(false);
        }
        return grpc::Status::OK;
    }

    grpc::Status handle_append_entries(const AppendEntriesRequest* req, AppendEntriesResponse* resp) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (req->term() > state_.current_term()) become_follower_locked(req->term());
        resp->set_term(state_.current_term());
        if (req->term() < state_.current_term()) { resp->set_success(false); return grpc::Status::OK; }

        role_ = Role::Follower;
        leader_id_ = req->leader_id();
        reset_election_deadline_locked();

        if (req->prev_log_index() > 0) {
            if (!log_.has_entry_at(req->prev_log_index()) ||
                log_.term_at(req->prev_log_index()) != req->prev_log_term()) {
                resp->set_success(false);
                return grpc::Status::OK;
            }
        }

        std::vector<PersistedEntry> new_entries;
        for (auto& e : req->entries()) new_entries.push_back({e.term(), e.command()});
        if (!new_entries.empty()) {
            log_.truncate_and_append(req->prev_log_index(), new_entries);
        }

        if (req->leader_commit() > commit_index_) {
            commit_index_ = std::min(req->leader_commit(), log_.last_index());
            apply_committed_entries_locked();
        }
        resp->set_success(true);
        return grpc::Status::OK;
    }

    struct Status {
        int node_id; std::string role; uint64_t term; int leader_id;
        uint64_t log_length; uint64_t commit_index;
        std::vector<std::string> applied_commands;
    };
    Status status() {
        std::lock_guard<std::mutex> lock(mutex_);
        const char* r = role_ == Role::Leader ? "leader" : role_ == Role::Candidate ? "candidate" : "follower";
        return {node_id_, r, state_.current_term(), leader_id_, log_.last_index(), commit_index_, applied_commands_};
    }

private:
    int node_id_;
    std::vector<RaftPeer> peers_;
    std::map<int, std::unique_ptr<RaftService::Stub>> stubs_;
    PersistentState state_;
    RaftLog log_;

    std::mutex mutex_;
    Role role_ = Role::Follower;
    int leader_id_ = -1;
    uint64_t commit_index_ = 0;
    uint64_t last_applied_ = 0;
    std::vector<std::string> applied_commands_; // the Phase 3b state machine: an ordered record
                                                 // of committed commands. Phase 3c replaces this
                                                 // with actually applying AddShard/RemoveShard.
    std::map<int, uint64_t> next_index_;
    std::map<int, uint64_t> match_index_;

    std::chrono::steady_clock::time_point election_deadline_;
    std::chrono::steady_clock::time_point next_heartbeat_;

    std::atomic<bool> running_{false};
    std::thread ticker_thread_;

    static constexpr int ELECTION_TIMEOUT_MIN_MS = 300;
    static constexpr int ELECTION_TIMEOUT_MAX_MS = 600;
    static constexpr int HEARTBEAT_INTERVAL_MS = 50;
    static constexpr int RPC_TIMEOUT_MS = 100;

    void become_follower_locked(uint64_t new_term) {
        state_.set(new_term, -1);
        role_ = Role::Follower;
    }

    void apply_committed_entries_locked() {
        while (last_applied_ < commit_index_) {
            last_applied_++;
            applied_commands_.push_back(log_.command_at(last_applied_));
        }
    }

    // The single trickiest safety rule in Raft (paper section 5.4.2,
    // "Figure 8"): see compute_new_commit_index above, which this calls
    // directly so the production path and the unit-tested path are the
    // exact same code, not a reimplementation that could drift.
    void advance_commit_index_locked() {
        uint64_t new_commit = compute_new_commit_index(
            log_.last_index(), match_index_, peers_.size(), commit_index_,
            state_.current_term(), [this](uint64_t idx) { return log_.term_at(idx); });
        if (new_commit > commit_index_) {
            commit_index_ = new_commit;
            apply_committed_entries_locked();
        }
    }

    void reset_election_deadline_locked() {
        static thread_local std::mt19937 rng(std::random_device{}());
        std::uniform_int_distribution<int> dist(ELECTION_TIMEOUT_MIN_MS, ELECTION_TIMEOUT_MAX_MS);
        election_deadline_ = std::chrono::steady_clock::now() + std::chrono::milliseconds(dist(rng));
    }
    void reset_election_deadline() {
        std::lock_guard<std::mutex> lock(mutex_);
        reset_election_deadline_locked();
    }

    void tick_loop() {
        while (running_) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            bool should_start_election = false, should_replicate = false;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                auto now = std::chrono::steady_clock::now();
                if (role_ != Role::Leader && now >= election_deadline_) {
                    should_start_election = true;
                } else if (role_ == Role::Leader && now >= next_heartbeat_) {
                    should_replicate = true;
                    next_heartbeat_ = now + std::chrono::milliseconds(HEARTBEAT_INTERVAL_MS);
                }
            }
            if (should_start_election) start_election();
            if (should_replicate) replicate_to_followers();
        }
    }

    void start_election() {
        uint64_t election_term, last_log_index, last_log_term;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            state_.set(state_.current_term() + 1, node_id_);
            role_ = Role::Candidate;
            leader_id_ = -1;
            election_term = state_.current_term();
            last_log_index = log_.last_index();
            last_log_term = log_.last_term();
            reset_election_deadline_locked();
        }

        std::vector<std::future<bool>> futures;
        for (auto& [peer_id, stub_ptr] : stubs_) {
            auto* stub = stub_ptr.get();
            futures.push_back(std::async(std::launch::async, [this, stub, election_term, last_log_index, last_log_term]() {
                RequestVoteRequest req;
                req.set_term(election_term);
                req.set_candidate_id(node_id_);
                req.set_last_log_index(last_log_index);
                req.set_last_log_term(last_log_term);
                grpc::ClientContext ctx;
                ctx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(RPC_TIMEOUT_MS));
                RequestVoteResponse resp;
                grpc::Status status = stub->RequestVote(&ctx, req, &resp);
                if (!status.ok()) return false;
                if (resp.term() > election_term) {
                    std::lock_guard<std::mutex> lock(mutex_);
                    if (resp.term() > state_.current_term()) become_follower_locked(resp.term());
                    return false;
                }
                return resp.vote_granted();
            }));
        }

        int votes = 1;
        for (auto& f : futures) if (f.get()) votes++;

        int majority = (int)(peers_.size() / 2) + 1;
        std::lock_guard<std::mutex> lock(mutex_);
        if (role_ == Role::Candidate && state_.current_term() == election_term && votes >= majority) {
            role_ = Role::Leader;
            leader_id_ = node_id_;
            uint64_t next = log_.last_index() + 1;
            next_index_.clear();
            match_index_.clear();
            for (auto& [peer_id, stub_ptr] : stubs_) {
                next_index_[peer_id] = next;
                match_index_[peer_id] = 0;
            }
            next_heartbeat_ = std::chrono::steady_clock::now();
        }
    }

    // Doubles as both heartbeat and log replication -- same RPC either
    // way, AppendEntries with possibly-empty entries. Runs on its own
    // thread for the same reason as Phase 3a: a slow peer can't be allowed
    // to delay the 10ms tick loop.
    void replicate_to_followers() {
        uint64_t term;
        std::map<int, uint64_t> next_index_snapshot;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (role_ != Role::Leader) return;
            term = state_.current_term();
            next_index_snapshot = next_index_;
        }
        std::thread([this, term, next_index_snapshot]() {
            std::vector<std::future<void>> futures;
            for (auto& [peer_id, stub_ptr] : stubs_) {
                auto* stub = stub_ptr.get();
                uint64_t next_idx = next_index_snapshot.at(peer_id);
                futures.push_back(std::async(std::launch::async, [this, stub, term, peer_id, next_idx]() {
                    send_append_entries_to_peer(stub, term, peer_id, next_idx);
                }));
            }
            for (auto& f : futures) f.get();
        }).detach();
    }

    void send_append_entries_to_peer(RaftService::Stub* stub, uint64_t term, int peer_id, uint64_t next_idx) {
        AppendEntriesRequest req;
        uint64_t prev_log_index = next_idx - 1;
        uint64_t prev_log_term;
        size_t n_entries_sent;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (role_ != Role::Leader || state_.current_term() != term) return;
            prev_log_term = log_.term_at(prev_log_index);
            req.set_term(term);
            req.set_leader_id(node_id_);
            req.set_prev_log_index(prev_log_index);
            req.set_prev_log_term(prev_log_term);
            req.set_leader_commit(commit_index_);
            for (uint64_t i = next_idx; i <= log_.last_index(); i++) {
                auto* e = req.add_entries();
                e->set_term(log_.term_at(i));
                e->set_index(i);
                e->set_command(log_.command_at(i));
            }
            n_entries_sent = (size_t)req.entries_size();
        }

        grpc::ClientContext ctx;
        ctx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(RPC_TIMEOUT_MS));
        AppendEntriesResponse resp;
        grpc::Status status = stub->AppendEntries(&ctx, req, &resp);
        if (!status.ok()) return;

        std::lock_guard<std::mutex> lock(mutex_);
        if (resp.term() > state_.current_term()) { become_follower_locked(resp.term()); return; }
        if (role_ != Role::Leader || state_.current_term() != term) return;

        if (resp.success()) {
            uint64_t new_match = prev_log_index + n_entries_sent;
            if (new_match > match_index_[peer_id]) match_index_[peer_id] = new_match;
            next_index_[peer_id] = new_match + 1;
            advance_commit_index_locked();
        } else if (next_index_[peer_id] > 1) {
            next_index_[peer_id]--;
        }
    }
};

} // namespace raft
} // namespace nanodb
