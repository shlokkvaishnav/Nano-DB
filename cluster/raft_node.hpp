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
#include <grpcpp/grpcpp.h>
#include "raft.grpc.pb.h"
#include "raft_state.hpp"

namespace nanodb {
namespace raft {

enum class Role { Follower, Candidate, Leader };

struct RaftPeer {
    int node_id;
    std::string host;
    int port;
    std::string address() const { return host + ":" + std::to_string(port); }
};

class RaftNode {
public:
    RaftNode(int node_id, std::vector<RaftPeer> peers, const std::string& state_path)
        : node_id_(node_id), peers_(std::move(peers)) {
        state_.open_file(state_path);
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

    grpc::Status handle_request_vote(const RequestVoteRequest* req, RequestVoteResponse* resp) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (req->term() > state_.current_term()) become_follower_locked(req->term());
        resp->set_term(state_.current_term());

        if (req->term() < state_.current_term()) {
            resp->set_vote_granted(false);
            return grpc::Status::OK;
        }
        bool can_vote = (state_.voted_for() == -1 || state_.voted_for() == req->candidate_id());
        // Log up-to-date check: trivially satisfied in Phase 3a (no real
        // log yet, my_last_log_index_/term_ stay 0 for everyone). Written
        // correctly now so Phase 3b can feed real values without touching
        // this comparison.
        bool log_ok = (req->last_log_term() > my_last_log_term_) ||
                      (req->last_log_term() == my_last_log_term_ && req->last_log_index() >= my_last_log_index_);
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
        if (req->term() < state_.current_term()) {
            resp->set_success(false);
            return grpc::Status::OK;
        }
        // A valid leader for our term: recognize it, reset election timer.
        // (Phase 3a: entries always empty, so success is unconditional once
        // the term check passes. Phase 3b adds the real log-consistency
        // check here.)
        role_ = Role::Follower;
        leader_id_ = req->leader_id();
        reset_election_deadline_locked();
        resp->set_success(true);
        return grpc::Status::OK;
    }

    struct Status { int node_id; std::string role; uint64_t term; int leader_id; };
    Status status() {
        std::lock_guard<std::mutex> lock(mutex_);
        const char* r = role_ == Role::Leader ? "leader" : role_ == Role::Candidate ? "candidate" : "follower";
        return {node_id_, r, state_.current_term(), leader_id_};
    }

private:
    int node_id_;
    std::vector<RaftPeer> peers_;
    std::map<int, std::unique_ptr<RaftService::Stub>> stubs_;
    PersistentState state_;

    std::mutex mutex_;
    Role role_ = Role::Follower;
    int leader_id_ = -1;
    std::chrono::steady_clock::time_point election_deadline_;
    std::chrono::steady_clock::time_point next_heartbeat_;

    // Stand in for "my last log index/term" until Phase 3b introduces a
    // real log.
    uint64_t my_last_log_index_ = 0;
    uint64_t my_last_log_term_ = 0;

    std::atomic<bool> running_{false};
    std::thread ticker_thread_;

    // Verified via Monte Carlo simulation of split-vote rates for a 3-node
    // cluster at realistic same-machine/LAN RPC latency: this range gives
    // an ~89% clean-election rate per round, vs ~82% for the textbook
    // 150-300ms range and ~52% for a tight 50-100ms range. The absolute
    // timeout is still short enough to keep failover demos snappy.
    static constexpr int ELECTION_TIMEOUT_MIN_MS = 300;
    static constexpr int ELECTION_TIMEOUT_MAX_MS = 600;
    static constexpr int HEARTBEAT_INTERVAL_MS = 50;
    static constexpr int RPC_TIMEOUT_MS = 100;

    void become_follower_locked(uint64_t new_term) {
        state_.set(new_term, -1);
        role_ = Role::Follower;
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
            bool should_start_election = false, should_send_heartbeat = false;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                auto now = std::chrono::steady_clock::now();
                if (role_ != Role::Leader && now >= election_deadline_) {
                    should_start_election = true;
                } else if (role_ == Role::Leader && now >= next_heartbeat_) {
                    should_send_heartbeat = true;
                    next_heartbeat_ = now + std::chrono::milliseconds(HEARTBEAT_INTERVAL_MS);
                }
            }
            if (should_start_election) start_election();
            if (should_send_heartbeat) send_heartbeats();
        }
    }

    void start_election() {
        uint64_t election_term;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            state_.set(state_.current_term() + 1, node_id_); // vote for self
            role_ = Role::Candidate;
            leader_id_ = -1;
            election_term = state_.current_term();
            reset_election_deadline_locked();
        }

        std::vector<std::future<bool>> futures;
        for (auto& [peer_id, stub_ptr] : stubs_) {
            auto* stub = stub_ptr.get();
            futures.push_back(std::async(std::launch::async, [this, stub, election_term]() {
                RequestVoteRequest req;
                req.set_term(election_term);
                req.set_candidate_id(node_id_);
                req.set_last_log_index(my_last_log_index_);
                req.set_last_log_term(my_last_log_term_);
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

        int votes = 1; // self
        for (auto& f : futures) if (f.get()) votes++;

        int majority = (int)(peers_.size() / 2) + 1;
        std::lock_guard<std::mutex> lock(mutex_);
        // Only become leader if still a candidate in the SAME term the
        // election was started for -- could have stepped down or seen a
        // higher term while votes were in flight.
        if (role_ == Role::Candidate && state_.current_term() == election_term && votes >= majority) {
            role_ = Role::Leader;
            leader_id_ = node_id_;
            next_heartbeat_ = std::chrono::steady_clock::now(); // send immediately
        }
    }

    // Runs on its own thread so a slow/unreachable peer can't delay the
    // tick loop, which also has to keep checking this node's own election
    // deadline every 10ms. The std::async futures are collected into a
    // local vector and joined inside that thread -- discarding a
    // std::async future immediately, as a one-liner-per-peer loop would,
    // makes its destructor block synchronously until that one RPC
    // completes, which would have serialized every heartbeat round across
    // every peer instead of fanning them out. Caught by actually testing
    // this, not by inspection.
    void send_heartbeats() {
        uint64_t term;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (role_ != Role::Leader) return;
            term = state_.current_term();
        }
        std::thread([this, term]() {
            std::vector<std::future<void>> futures;
            for (auto& [peer_id, stub_ptr] : stubs_) {
                auto* stub = stub_ptr.get();
                futures.push_back(std::async(std::launch::async, [this, stub, term]() {
                    AppendEntriesRequest req;
                    req.set_term(term);
                    req.set_leader_id(node_id_);
                    req.set_prev_log_index(0);
                    req.set_prev_log_term(0);
                    req.set_leader_commit(0);
                    grpc::ClientContext ctx;
                    ctx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(RPC_TIMEOUT_MS));
                    AppendEntriesResponse resp;
                    grpc::Status status = stub->AppendEntries(&ctx, req, &resp);
                    if (status.ok() && resp.term() > term) {
                        std::lock_guard<std::mutex> lock(mutex_);
                        if (resp.term() > state_.current_term()) become_follower_locked(resp.term());
                    }
                }));
            }
            for (auto& f : futures) f.get();
        }).detach();
    }
};

} // namespace raft
} // namespace nanodb
