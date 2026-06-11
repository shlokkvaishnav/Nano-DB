#include <iostream>
#include <string>
#include <csignal>
#include <cstdlib>

#include "httplib.h"
#include "json.hpp"

#include "../include/config/constants.hpp"
#include "../include/config/types.hpp"
#include "../include/storage/memory_map.hpp"
#include "../include/index/hnsw.hpp"

using json = nlohmann::json;
using namespace nanodb;

static httplib::Server* g_server = nullptr;

void signal_handler(int) {
    if (g_server) g_server->stop();
}

static const char* metric_to_string(DistanceMetric m) {
    switch (m) {
        case DistanceMetric::L2: return "L2";
        case DistanceMetric::Cosine: return "Cosine";
        case DistanceMetric::InnerProduct: return "InnerProduct";
    }
    return "Unknown";
}

int main() {
    // Configuration from environment
    const char* port_env = std::getenv("NANODB_PORT");
    int port = port_env ? std::atoi(port_env) : 8080;

    const char* data_dir_env = std::getenv("NANODB_DATA_DIR");
    std::string data_dir = data_dir_env ? data_dir_env : "data";

    std::string db_path = data_dir + "/index.ndb";
    std::string meta_path = data_dir + "/metadata.bin";

    // Initialize storage and index
    MMapHandler storage;
    try {
        storage.open_file(db_path, 100 * 1024 * 1024); // 100MB pre-allocation
    } catch (const std::exception& e) {
        std::cerr << "[ERROR] Failed to open storage: " << e.what() << std::endl;
        return 1;
    }

    HNSW index(storage, meta_path);

    // Set up HTTP server
    httplib::Server server;
    g_server = &server;

    std::signal(SIGINT, signal_handler);
    std::signal(SIGTERM, signal_handler);

    // POST /vectors — insert a vector
    server.Post("/vectors", [&](const httplib::Request& req, httplib::Response& res) {
        try {
            auto body = json::parse(req.body);

            if (!body.contains("id") || !body.contains("vector")) {
                res.status = 400;
                res.set_content(R"({"error":"missing required fields: id, vector"})", "application/json");
                return;
            }

            id_t id = body["id"].get<id_t>();
            auto vec_json = body["vector"];

            if (vec_json.size() != config::VECTOR_DIM) {
                res.status = 400;
                res.set_content(
                    R"({"error":"vector dimension mismatch, expected )" + std::to_string(config::VECTOR_DIM) + R"("})",
                    "application/json");
                return;
            }

            std::vector<float> vec(config::VECTOR_DIM);
            for (size_t i = 0; i < config::VECTOR_DIM; ++i) {
                vec[i] = vec_json[i].get<float>();
            }

            std::string metadata = body.value("metadata", "");

            index.insert(vec, id, metadata);

            res.status = 201;
            res.set_content(R"({"status":"ok","id":)" + std::to_string(id) + "}", "application/json");

        } catch (const json::exception& e) {
            res.status = 400;
            res.set_content(std::string(R"({"error":"invalid JSON: )") + e.what() + R"("})", "application/json");
        }
    });

    // POST /search — query nearest neighbors
    server.Post("/search", [&](const httplib::Request& req, httplib::Response& res) {
        try {
            auto body = json::parse(req.body);

            if (!body.contains("vector") || !body.contains("k")) {
                res.status = 400;
                res.set_content(R"({"error":"missing required fields: vector, k"})", "application/json");
                return;
            }

            auto vec_json = body["vector"];
            int k = body["k"].get<int>();

            if (vec_json.size() != config::VECTOR_DIM) {
                res.status = 400;
                res.set_content(
                    R"({"error":"vector dimension mismatch, expected )" + std::to_string(config::VECTOR_DIM) + R"("})",
                    "application/json");
                return;
            }

            if (k <= 0) {
                res.status = 400;
                res.set_content(R"({"error":"k must be positive"})", "application/json");
                return;
            }

            std::vector<float> vec(config::VECTOR_DIM);
            for (size_t i = 0; i < config::VECTOR_DIM; ++i) {
                vec[i] = vec_json[i].get<float>();
            }

            auto results = index.search(vec, k);

            json results_json = json::array();
            for (const auto& r : results) {
                results_json.push_back({
                    {"id", r.id},
                    {"distance", r.distance},
                    {"metadata", r.metadata}
                });
            }

            json response = {{"results", results_json}};
            res.set_content(response.dump(), "application/json");

        } catch (const json::exception& e) {
            res.status = 400;
            res.set_content(std::string(R"({"error":"invalid JSON: )") + e.what() + R"("})", "application/json");
        }
    });

    // DELETE /vectors/:id — tombstone deletion
    server.Delete(R"(/vectors/(\d+))", [&](const httplib::Request& req, httplib::Response& res) {
        id_t id = std::stoul(req.matches[1]);
        index.delete_vector(id);
        res.set_content(R"({"status":"ok","id":)" + std::to_string(id) + "}", "application/json");
    });

    // GET /stats — index statistics
    server.Get("/stats", [&](const httplib::Request&, httplib::Response& res) {
        json stats = {
            {"element_count", index.size()},
            {"vector_dim", config::VECTOR_DIM},
            {"metric", metric_to_string(index.metric())}
        };
        res.set_content(stats.dump(), "application/json");
    });

    std::cout << "[NanoDB] Server listening on 0.0.0.0:" << port << std::endl;
    std::cout << "[NanoDB] Data directory: " << data_dir << std::endl;
    std::cout << "[NanoDB] Vector dimension: " << config::VECTOR_DIM << std::endl;

    server.listen("0.0.0.0", port);

    storage.close_file();
    std::cout << "[NanoDB] Server stopped." << std::endl;
    return 0;
}
