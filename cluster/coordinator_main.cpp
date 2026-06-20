#include <iostream>
#include <csignal>
#include <cstdlib>
#include <future>
#include <vector>
#include <algorithm>
#include <chrono>
#include <mutex>
#include <shared_mutex>
#include <atomic>
#include <set>

#include "httplib.h"
#include "json.hpp"
#include <grpcpp/grpcpp.h>

#include "nanodb_cluster.grpc.pb.h"
#include "cluster_config.hpp"
#include "routing.hpp"
#include "hash_ring.hpp"
#include "raft_node.hpp"
#include "raft_config.hpp"
#include "raft_service_impl.hpp"

using json = nlohmann::json;
using namespace nanodb::cluster;

static httplib::Server* g_server = nullptr;
static std::unique_ptr<nanodb::raft::RaftNode> g_raft_node;
static grpc::Server* g_raft_server = nullptr;
static constexpr int RPC_TIMEOUT_MS = 800;
static constexpr int MIGRATION_RPC_TIMEOUT_MS = 2000;

static std::atomic<bool> g_apply_poller_running{false};

void signal_handler(int) {
    if (g_server) g_server->stop();
    if (g_raft_node) g_raft_node->stop();
    if (g_raft_server) g_raft_server->Shutdown();
    g_apply_poller_running = false;
}

struct ShardClient {
    int shard_id;
    std::string host;
    int port;
    std::shared_ptr<grpc::Channel> channel;
    std::unique_ptr<ShardService::Stub> stub;
    std::atomic<bool> active{true};
};

// All cluster membership state lives behind this lock. Readers (the normal
// Insert/Search/Delete/Stats handlers) take a shared_lock just long enough
// to copy what they need -- a HashRing (cheap, plain data) or a list of raw
// ShardClient* (cheap, g_shards is append-only so the pointers stay valid
// forever) -- then release the lock before making any gRPC calls. Holding
// the lock across network I/O would serialize all cluster traffic. The
// rebalance handlers take a unique_lock only for the moments they actually
// append to g_shards or swap active_ring; the migration RPCs themselves run
// outside the lock.
static std::shared_mutex cluster_mutex;
static std::vector<std::unique_ptr<ShardClient>> g_shards;
static HashRing active_ring;
static std::atomic<bool> rebalancing{false};
static std::string g_cluster_config_path;

// Serializes apply_pending_raft_commands() across callers (the dedicated
// poller thread and any admin handler doing a synchronous catch-up after
// its own propose() commits), and tracks how many of RaftNode's applied
// commands have already been reflected in g_shards/active_ring.
static std::mutex g_apply_mutex;
static uint64_t g_local_applied_count = 0;

static std::unique_ptr<ShardClient> make_shard_client(int shard_id, const std::string& host, int port) {
    auto sc = std::make_unique<ShardClient>();
    sc->shard_id = shard_id;
    sc->host = host;
    sc->port = port;
    sc->channel = grpc::CreateChannel(host + ":" + std::to_string(port), grpc::InsecureChannelCredentials());
    sc->stub = ShardService::NewStub(sc->channel);
    return sc;
}

// Safe to use after the lock is released: g_shards is append-only, so a
// ShardClient's address never changes or becomes invalid once added.
static std::vector<ShardClient*> live_shards() {
    std::shared_lock lock(cluster_mutex);
    std::vector<ShardClient*> out;
    for (auto& sc : g_shards) {
        if (sc->active) out.push_back(sc.get());
    }
    return out;
}

static HashRing ring_snapshot() {
    std::shared_lock lock(cluster_mutex);
    return active_ring;
}

static ShardClient* find_shard(const std::vector<ShardClient*>& pool, int shard_id) {
    for (auto* sc : pool) {
        if (sc->shard_id == shard_id) return sc;
    }
    return nullptr;
}

static void persist_cluster_state() {
    std::vector<ShardEndpoint> eps;
    for (auto* sc : live_shards()) {
        eps.push_back({sc->shard_id, sc->host, sc->port});
    }
    try {
        save_cluster_config(g_cluster_config_path, eps);
    } catch (const std::exception& e) {
        std::cerr << "[Coordinator] WARNING: failed to persist cluster config: " << e.what() << std::endl;
    }
}

// The Phase 3c state machine: applies any newly Raft-committed
// AddShard/RemoveShard commands to g_shards/active_ring. Safe to call from
// any node regardless of role (followers need this too, to keep their own
// view in sync) and safe to call redundantly (no-ops if nothing new).
// Called both by a dedicated background poller (so followers stay in sync
// passively) and synchronously by the leader's admin handlers right after
// their own propose() commits, so migration logic never has to wait for
// the poller's cadence to see the change it just made.
//
// RemoveShard intentionally does NOT need special-case handling for
// "migration not done yet": by design (see coordinator_main.cpp's
// /admin/shards/remove), the leader only proposes RemoveShard AFTER
// migration has already completed, so by the time any node -- leader or
// follower -- applies this command, it's always safe to immediately
// exclude the shard from search/stats (active=false) as well as from
// future write routing (active_ring). There's no version of this command
// that gets applied before its data has moved.
static void apply_pending_raft_commands() {
    if (!g_raft_node) return;
    std::lock_guard<std::mutex> apply_lock(g_apply_mutex);
    auto st = g_raft_node->status();
    if (st.applied_commands.size() <= g_local_applied_count) return;

    for (uint64_t i = g_local_applied_count; i < st.applied_commands.size(); i++) {
        try {
            json cmd = json::parse(st.applied_commands[i]);
            std::string type = cmd.at("type").get<std::string>();

            if (type == "add_shard") {
                int shard_id = cmd.at("shard_id").get<int>();
                std::string host = cmd.at("host").get<std::string>();
                int port = cmd.at("port").get<int>();

                bool exists = false;
                {
                    std::shared_lock lock(cluster_mutex);
                    for (auto& sc : g_shards) if (sc->shard_id == shard_id) exists = true;
                }
                if (!exists) {
                    auto new_client = make_shard_client(shard_id, host, port);
                    std::unique_lock lock(cluster_mutex);
                    g_shards.push_back(std::move(new_client));
                    std::vector<int> ids;
                    for (auto& sc : g_shards) if (sc->active) ids.push_back(sc->shard_id);
                    active_ring.build(ids);
                }
            } else if (type == "remove_shard") {
                int shard_id = cmd.at("shard_id").get<int>();
                std::unique_lock lock(cluster_mutex);
                for (auto& sc : g_shards) {
                    if (sc->shard_id == shard_id) sc->active = false;
                }
                std::vector<int> ids;
                for (auto& sc : g_shards) if (sc->active) ids.push_back(sc->shard_id);
                active_ring.build(ids);
            } else {
                std::cerr << "[Coordinator] WARNING: unknown raft command type \"" << type << "\"" << std::endl;
            }
        } catch (const std::exception& e) {
            std::cerr << "[Coordinator] WARNING: failed to apply raft command #" << i << ": " << e.what() << std::endl;
        }
    }
    g_local_applied_count = st.applied_commands.size();
}

// Moves one key from source to dest. Insert-into-destination happens before
// delete-from-source, always -- a duplicate-for-a-moment is fine (search
// dedupes by external_id), a vector that's briefly on neither shard is not.
// Returns false if any step failed; the key is left wherever it last
// successfully existed (never silently dropped).
static bool migrate_one_key(ShardClient& source, ShardClient& dest, const std::string& external_id) {
    GetVectorRequest greq;
    greq.set_external_id(external_id);
    grpc::ClientContext gctx;
    gctx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(MIGRATION_RPC_TIMEOUT_MS));
    GetVectorResponse gres;
    if (!source.stub->GetVector(&gctx, greq, &gres).ok() || !gres.ok()) return false;

    InsertRequest ireq;
    ireq.set_external_id(external_id);
    for (float f : gres.vector()) ireq.add_vector(f);
    ireq.set_metadata(gres.metadata());
    grpc::ClientContext ictx;
    ictx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(MIGRATION_RPC_TIMEOUT_MS));
    InsertResponse ires;
    if (!dest.stub->Insert(&ictx, ireq, &ires).ok() || !ires.ok()) return false;

    DeleteRequest dreq;
    dreq.set_external_id(external_id);
    grpc::ClientContext dctx;
    dctx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(MIGRATION_RPC_TIMEOUT_MS));
    DeleteResponse dres;
    source.stub->Delete(&dctx, dreq, &dres); // best-effort: if this fails the key is duplicated, not lost
    return true;
}

int main() {
    const char* config_env = std::getenv("NANODB_CLUSTER_CONFIG");
    g_cluster_config_path = config_env ? config_env : "deploy/cluster.local.json";

    const char* port_env = std::getenv("NANODB_HTTP_PORT");
    int http_port = port_env ? std::atoi(port_env) : 8080;

    std::vector<ShardEndpoint> endpoints;
    try {
        endpoints = load_cluster_config(g_cluster_config_path);
    } catch (const std::exception& e) {
        std::cerr << "[ERROR] " << e.what() << std::endl;
        return 1;
    }

    std::vector<int> ids;
    for (const auto& ep : endpoints) {
        g_shards.push_back(make_shard_client(ep.shard_id, ep.host, ep.port));
        ids.push_back(ep.shard_id);
    }
    active_ring.build(ids);
    std::cout << "[Coordinator] Loaded " << g_shards.size() << " shard(s) from " << g_cluster_config_path << std::endl;

    std::unique_ptr<nanodb::raft::RaftServiceImpl> raft_service;
    std::unique_ptr<grpc::Server> raft_server;
    std::thread raft_server_thread;

    const char* raft_node_id_env = std::getenv("NANODB_RAFT_NODE_ID");
    const char* raft_peers_env = std::getenv("NANODB_RAFT_PEERS_CONFIG");
    if (raft_node_id_env && raft_peers_env) {
        int raft_node_id = std::atoi(raft_node_id_env);
        const char* raft_state_env = std::getenv("NANODB_RAFT_STATE_PATH");
        std::string raft_state_path = raft_state_env ? raft_state_env : "raft_state.bin";

        std::vector<nanodb::raft::RaftPeer> raft_peers;
        try {
            raft_peers = nanodb::raft::load_raft_peers(raft_peers_env);
        } catch (const std::exception& e) {
            std::cerr << "[ERROR] " << e.what() << std::endl;
            return 1;
        }

        nanodb::raft::RaftPeer self_peer;
        bool found_self = false;
        for (auto& p : raft_peers) {
            if (p.node_id == raft_node_id) { self_peer = p; found_self = true; }
        }
        if (!found_self) {
            std::cerr << "[ERROR] NANODB_RAFT_NODE_ID=" << raft_node_id
                      << " not present in " << raft_peers_env << std::endl;
            return 1;
        }

        const char* raft_log_env = std::getenv("NANODB_RAFT_LOG_PATH");
        std::string raft_log_path = raft_log_env ? raft_log_env : "raft_log.bin";
        g_raft_node = std::make_unique<nanodb::raft::RaftNode>(raft_node_id, raft_peers, raft_state_path, raft_log_path);
        raft_service = std::make_unique<nanodb::raft::RaftServiceImpl>(*g_raft_node);

        grpc::ServerBuilder raft_builder;
        raft_builder.AddListeningPort(self_peer.host + ":" + std::to_string(self_peer.port),
                                       grpc::InsecureServerCredentials());
        raft_builder.RegisterService(raft_service.get());
        raft_server = raft_builder.BuildAndStart();
        g_raft_server = raft_server.get();
        raft_server_thread = std::thread([&raft_server]() { raft_server->Wait(); });

        g_raft_node->start();
        std::cout << "[Coordinator] Raft node " << raft_node_id << " listening on "
                  << self_peer.host << ":" << self_peer.port << std::endl;
    } else {
        std::cout << "[Coordinator] Raft disabled (set NANODB_RAFT_NODE_ID and "
                     "NANODB_RAFT_PEERS_CONFIG to enable)" << std::endl;
    }

    std::thread apply_poller_thread;
    if (g_raft_node) {
        g_apply_poller_running = true;
        apply_poller_thread = std::thread([&]() {
            while (g_apply_poller_running) {
                apply_pending_raft_commands();
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
        });
    }

    httplib::Server server;
    g_server = &server;
    std::signal(SIGINT, signal_handler);
    std::signal(SIGTERM, signal_handler);

    server.Post("/vectors", [&](const httplib::Request& req, httplib::Response& res) {
        if (rebalancing) {
            res.status = 503;
            res.set_content(R"({"error":"cluster is rebalancing, try again shortly"})", "application/json");
            return;
        }
        try {
            auto body = json::parse(req.body);
            if (!body.contains("id") || !body.contains("vector")) {
                res.status = 400;
                res.set_content(R"({"error":"missing required fields: id, vector"})", "application/json");
                return;
            }
            std::string external_id = body["id"].is_string()
                ? body["id"].get<std::string>()
                : std::to_string(body["id"].get<long long>());

            HashRing ring = ring_snapshot();
            auto pool = live_shards();
            ShardClient* dest = find_shard(pool, ring.route(external_id));
            if (!dest) {
                res.status = 503;
                res.set_content(R"({"error":"destination shard unavailable"})", "application/json");
                return;
            }

            InsertRequest grpc_req;
            grpc_req.set_external_id(external_id);
            for (const auto& v : body["vector"]) grpc_req.add_vector(v.get<float>());
            grpc_req.set_metadata(body.value("metadata", ""));

            grpc::ClientContext ctx;
            ctx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(RPC_TIMEOUT_MS));
            InsertResponse grpc_res;
            grpc::Status status = dest->stub->Insert(&ctx, grpc_req, &grpc_res);

            if (!status.ok() || !grpc_res.ok()) {
                res.status = 502;
                json err = {{"error", "shard " + std::to_string(dest->shard_id) + " insert failed: " +
                                       (status.ok() ? grpc_res.error() : status.error_message())}};
                res.set_content(err.dump(), "application/json");
                return;
            }
            res.status = 201;
            json ok = {{"status", "ok"}, {"id", external_id}, {"shard", dest->shard_id}};
            res.set_content(ok.dump(), "application/json");
        } catch (const json::exception& e) {
            res.status = 400;
            res.set_content(std::string(R"({"error":"invalid JSON: )") + e.what() + R"("})", "application/json");
        }
    });

    server.Post("/search", [&](const httplib::Request& req, httplib::Response& res) {
        try {
            auto body = json::parse(req.body);
            if (!body.contains("vector") || !body.contains("k")) {
                res.status = 400;
                res.set_content(R"({"error":"missing required fields: vector, k"})", "application/json");
                return;
            }
            int k = body["k"].get<int>();
            std::vector<float> vec;
            for (const auto& v : body["vector"]) vec.push_back(v.get<float>());

            auto pool = live_shards();
            std::vector<std::future<std::pair<int, SearchResponse>>> futures;
            for (auto* sc : pool) {
                futures.push_back(std::async(std::launch::async, [sc, vec, k]() {
                    SearchRequest grpc_req;
                    for (float f : vec) grpc_req.add_vector(f);
                    grpc_req.set_k(k);
                    grpc::ClientContext ctx;
                    ctx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(RPC_TIMEOUT_MS));
                    SearchResponse grpc_res;
                    grpc::Status status = sc->stub->Search(&ctx, grpc_req, &grpc_res);
                    if (!status.ok()) grpc_res.set_ok(false);
                    return std::make_pair(sc->shard_id, grpc_res);
                }));
            }

            std::vector<json> merged;
            std::vector<int> unavailable;
            for (auto& fut : futures) {
                auto [shard_id, grpc_res] = fut.get();
                if (!grpc_res.ok()) { unavailable.push_back(shard_id); continue; }
                for (const auto& r : grpc_res.results()) {
                    merged.push_back({{"id", r.external_id()}, {"distance", r.distance()}, {"metadata", r.metadata()}});
                }
            }
            std::sort(merged.begin(), merged.end(), [](const json& a, const json& b) {
                return a["distance"].get<float>() < b["distance"].get<float>();
            });

            // Dedupe by id, keep the first (lowest-distance) occurrence. A
            // key mid-migration can briefly exist on two shards; this is the
            // free fix for that, independent of whether a rebalance is
            // actually running right now.
            std::vector<json> deduped;
            std::set<std::string> seen;
            for (auto& r : merged) {
                std::string id = r["id"].get<std::string>();
                if (seen.count(id)) continue;
                seen.insert(id);
                deduped.push_back(r);
                if (deduped.size() >= (size_t)k) break;
            }

            json response = {{"results", deduped}};
            if (!unavailable.empty()) {
                response["degraded"] = true;
                response["unavailable_shards"] = unavailable;
            }
            res.set_content(response.dump(), "application/json");
        } catch (const json::exception& e) {
            res.status = 400;
            res.set_content(std::string(R"({"error":"invalid JSON: )") + e.what() + R"("})", "application/json");
        }
    });

    server.Delete(R"(/vectors/(.+))", [&](const httplib::Request& req, httplib::Response& res) {
        if (rebalancing) {
            res.status = 503;
            res.set_content(R"({"error":"cluster is rebalancing, try again shortly"})", "application/json");
            return;
        }
        std::string external_id = req.matches[1];
        HashRing ring = ring_snapshot();
        auto pool = live_shards();
        ShardClient* dest = find_shard(pool, ring.route(external_id));
        if (!dest) {
            res.status = 503;
            res.set_content(R"({"error":"destination shard unavailable"})", "application/json");
            return;
        }
        DeleteRequest grpc_req;
        grpc_req.set_external_id(external_id);
        grpc::ClientContext ctx;
        ctx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(RPC_TIMEOUT_MS));
        DeleteResponse grpc_res;
        grpc::Status status = dest->stub->Delete(&ctx, grpc_req, &grpc_res);
        if (!status.ok() || !grpc_res.ok()) {
            res.status = 404;
            res.set_content(R"({"error":"not found or shard unreachable"})", "application/json");
            return;
        }
        res.set_content(R"({"status":"ok","id":")" + external_id + "\"}", "application/json");
    });

    server.Get("/stats", [&](const httplib::Request&, httplib::Response& res) {
        auto pool = live_shards();
        json per_shard = json::array();
        uint64_t total = 0;
        std::vector<int> unavailable;
        for (auto* sc : pool) {
            StatsRequest grpc_req;
            grpc::ClientContext ctx;
            ctx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(RPC_TIMEOUT_MS));
            StatsResponse grpc_res;
            grpc::Status status = sc->stub->Stats(&ctx, grpc_req, &grpc_res);
            if (!status.ok()) { unavailable.push_back(sc->shard_id); continue; }
            per_shard.push_back({{"shard_id", sc->shard_id}, {"element_count", grpc_res.element_count()}});
            total += grpc_res.element_count();
        }
        json response = {{"total_element_count", total}, {"shards", per_shard}, {"num_shards", pool.size()}};
        if (!unavailable.empty()) {
            response["degraded"] = true;
            response["unavailable_shards"] = unavailable;
        }
        res.set_content(response.dump(), "application/json");
    });

    server.Get("/raft/status", [&](const httplib::Request&, httplib::Response& res) {
        if (!g_raft_node) {
            res.status = 404;
            res.set_content(R"({"error":"raft is not enabled on this coordinator"})", "application/json");
            return;
        }
        auto st = g_raft_node->status();
        json response = {{"node_id", st.node_id}, {"role", st.role},
                          {"term", st.term}, {"leader_id", st.leader_id},
                          {"log_length", st.log_length}, {"commit_index", st.commit_index},
                          {"applied_commands", st.applied_commands}};
        res.set_content(response.dump(), "application/json");
    });

    server.Post("/raft/propose", [&](const httplib::Request& req, httplib::Response& res) {
        if (!g_raft_node) {
            res.status = 404;
            res.set_content(R"({"error":"raft is not enabled on this coordinator"})", "application/json");
            return;
        }
        bool ok = g_raft_node->propose(req.body);
        json response = {{"committed", ok}};
        res.status = ok ? 200 : 503;
        res.set_content(response.dump(), "application/json");
    });

    // POST /admin/shards/add  body: {"shard_id": 3, "host": "shard-3", "port": 9090}
    server.Post("/admin/shards/add", [&](const httplib::Request& req, httplib::Response& res) {
        if (!g_raft_node) {
            res.status = 400;
            res.set_content(R"({"error":"raft is not enabled on this coordinator"})", "application/json");
            return;
        }
        auto raft_st = g_raft_node->status();
        if (raft_st.role != "leader") {
            res.status = 503;
            json err = {{"error", "not the leader"}, {"leader_id", raft_st.leader_id}};
            res.set_content(err.dump(), "application/json");
            return;
        }

        bool expected = false;
        if (!rebalancing.compare_exchange_strong(expected, true)) {
            res.status = 409;
            res.set_content(R"({"error":"a rebalance is already in progress"})", "application/json");
            return;
        }
        try {
            auto body = json::parse(req.body);
            int new_id = body.at("shard_id").get<int>();
            std::string host = body.at("host").get<std::string>();
            int port = body.at("port").get<int>();

            // Propose first: a shard can't be migrated TO until it has a
            // ShardClient/stub, which only exists once the AddShard command
            // has actually been applied.
            json cmd = {{"type", "add_shard"}, {"shard_id", new_id}, {"host", host}, {"port", port}};
            if (!g_raft_node->propose(cmd.dump())) {
                rebalancing = false;
                res.status = 503;
                res.set_content(R"({"error":"raft proposal did not commit, lost leadership or timed out"})", "application/json");
                return;
            }
            apply_pending_raft_commands(); // don't wait for the poller's cadence

            auto pool = live_shards();
            ShardClient* new_ptr = find_shard(pool, new_id);
            if (!new_ptr) {
                rebalancing = false;
                res.status = 500;
                res.set_content(R"({"error":"committed via raft but not present locally after apply -- this should not happen"})", "application/json");
                return;
            }
            HashRing new_ring = ring_snapshot();

            int migrated = 0, failed = 0;
            for (auto* source : pool) {
                if (source->shard_id == new_id) continue;
                ListLocalIdsRequest lreq;
                grpc::ClientContext lctx;
                lctx.set_deadline(std::chrono::system_clock::now() + std::chrono::seconds(10));
                ListLocalIdsResponse lres;
                if (!source->stub->ListLocalIds(&lctx, lreq, &lres).ok()) continue;

                for (const auto& external_id : lres.external_ids()) {
                    if (new_ring.route(external_id) != new_id) continue;
                    if (migrate_one_key(*source, *new_ptr, external_id)) migrated++;
                    else failed++;
                }
            }

            persist_cluster_state();
            rebalancing = false;

            json ok = {{"status", "ok"}, {"shard_id", new_id}, {"keys_migrated", migrated}, {"keys_failed", failed}};
            res.set_content(ok.dump(), "application/json");
        } catch (const std::exception& e) {
            rebalancing = false;
            res.status = 400;
            json err = {{"error", e.what()}};
            res.set_content(err.dump(), "application/json");
        }
    });

    // POST /admin/shards/remove  body: {"shard_id": 1}
    server.Post("/admin/shards/remove", [&](const httplib::Request& req, httplib::Response& res) {
        if (!g_raft_node) {
            res.status = 400;
            res.set_content(R"({"error":"raft is not enabled on this coordinator"})", "application/json");
            return;
        }
        auto raft_st = g_raft_node->status();
        if (raft_st.role != "leader") {
            res.status = 503;
            json err = {{"error", "not the leader"}, {"leader_id", raft_st.leader_id}};
            res.set_content(err.dump(), "application/json");
            return;
        }

        bool expected = false;
        if (!rebalancing.compare_exchange_strong(expected, true)) {
            res.status = 409;
            res.set_content(R"({"error":"a rebalance is already in progress"})", "application/json");
            return;
        }
        try {
            auto body = json::parse(req.body);
            int leaving_id = body.at("shard_id").get<int>();

            auto pool = live_shards();
            ShardClient* leaving = find_shard(pool, leaving_id);
            if (!leaving) {
                rebalancing = false;
                res.status = 404;
                res.set_content(R"({"error":"shard not found or already inactive"})", "application/json");
                return;
            }
            if (pool.size() <= 1) {
                rebalancing = false;
                res.status = 400;
                res.set_content(R"({"error":"cannot remove the last shard"})", "application/json");
                return;
            }

            // Migrate BEFORE proposing, deliberately: the leaving shard
            // already has a live stub (it's still in g_shards), so unlike
            // AddShard there's no technical need to commit first. Moving
            // the data first and proposing RemoveShard as the atomic
            // "officially gone" declaration afterward means that by the
            // time ANY node -- leader or follower -- applies this command,
            // the migration is already done, so it's always safe for that
            // application to immediately mark the shard inactive (excluded
            // from search/stats) with no window where data is invisible
            // because it's "removed" on paper but not actually moved yet.
            std::vector<int> remaining_ids;
            for (auto* sc : pool) if (sc->shard_id != leaving_id) remaining_ids.push_back(sc->shard_id);
            HashRing post_remove_ring(remaining_ids);

            int migrated = 0, failed = 0;
            ListLocalIdsRequest lreq;
            grpc::ClientContext lctx;
            lctx.set_deadline(std::chrono::system_clock::now() + std::chrono::seconds(10));
            ListLocalIdsResponse lres;
            if (leaving->stub->ListLocalIds(&lctx, lreq, &lres).ok()) {
                for (const auto& external_id : lres.external_ids()) {
                    ShardClient* dest = find_shard(pool, post_remove_ring.route(external_id));
                    if (!dest) { failed++; continue; }
                    if (migrate_one_key(*leaving, *dest, external_id)) migrated++;
                    else failed++;
                }
            }

            json cmd = {{"type", "remove_shard"}, {"shard_id", leaving_id}};
            if (!g_raft_node->propose(cmd.dump())) {
                rebalancing = false;
                res.status = 503;
                res.set_content(R"({"error":"data was migrated but the raft proposal to finalize removal did not commit -- retry the remove, migration is idempotent"})", "application/json");
                return;
            }
            apply_pending_raft_commands();
            persist_cluster_state();
            rebalancing = false;

            json ok = {{"status", "ok"}, {"shard_id", leaving_id}, {"keys_migrated", migrated}, {"keys_failed", failed}};
            res.set_content(ok.dump(), "application/json");
        } catch (const std::exception& e) {
            rebalancing = false;
            res.status = 400;
            json err = {{"error", e.what()}};
            res.set_content(err.dump(), "application/json");
        }
    });

    std::cout << "[Coordinator] Listening on 0.0.0.0:" << http_port << std::endl;
    server.listen("0.0.0.0", http_port);
    if (raft_server_thread.joinable()) raft_server_thread.join();
    if (apply_poller_thread.joinable()) apply_poller_thread.join();
    std::cout << "[Coordinator] Stopped." << std::endl;
    return 0;
}
