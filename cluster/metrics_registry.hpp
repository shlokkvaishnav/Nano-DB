#pragma once
#include <atomic>
#include <string>
#include <vector>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <chrono>
#include <cmath>
#include <cstring>
#include <set>

namespace nanodb { namespace metrics {

class Counter {
public:
    void inc(uint64_t n = 1) { value_.fetch_add(n, std::memory_order_relaxed); }
    uint64_t value() const { return value_.load(std::memory_order_relaxed); }
private:
    std::atomic<uint64_t> value_{0};
};

class Gauge {
public:
    void set(int64_t v) { value_.store(v, std::memory_order_relaxed); }
    void inc(int64_t n = 1) { value_.fetch_add(n, std::memory_order_relaxed); }
    void dec(int64_t n = 1) { value_.fetch_sub(n, std::memory_order_relaxed); }
    int64_t value() const { return value_.load(std::memory_order_relaxed); }
private:
    std::atomic<int64_t> value_{0};
};

class Histogram {
public:
    explicit Histogram(std::vector<double> buckets = default_buckets())
        : bounds_(std::move(buckets)) {
        counts_ = std::make_unique<std::atomic<uint64_t>[]>(bounds_.size() + 1);
        for (size_t i = 0; i <= bounds_.size(); ++i) counts_[i].store(0);
    }

    void observe(double value) {
        uint64_t old_bits = sum_.load(std::memory_order_relaxed);
        while (!sum_.compare_exchange_weak(old_bits,
            to_bits(from_bits(old_bits) + value), std::memory_order_relaxed)) {}
        count_.fetch_add(1, std::memory_order_relaxed);
        for (size_t i = 0; i < bounds_.size(); ++i) {
            if (value <= bounds_[i]) {
                counts_[i].fetch_add(1, std::memory_order_relaxed);
                return;
            }
        }
        counts_[bounds_.size()].fetch_add(1, std::memory_order_relaxed);
    }

    uint64_t count() const { return count_.load(std::memory_order_relaxed); }
    double sum() const { return from_bits(sum_.load(std::memory_order_relaxed)); }
    const std::vector<double>& bounds() const { return bounds_; }
    uint64_t bucket_count(size_t i) const { return counts_[i].load(std::memory_order_relaxed); }
    size_t num_buckets() const { return bounds_.size() + 1; }

    static std::vector<double> default_buckets() {
        return {0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0};
    }

private:
    static uint64_t to_bits(double v) { uint64_t b; std::memcpy(&b, &v, 8); return b; }
    static double from_bits(uint64_t b) { double v; std::memcpy(&v, &b, 8); return v; }

    std::vector<double> bounds_;
    std::unique_ptr<std::atomic<uint64_t>[]> counts_;
    std::atomic<uint64_t> count_{0};
    std::atomic<uint64_t> sum_{0};
};

struct MetricMeta {
    std::string name;
    std::string help;
    std::string type;
    std::string labels;
};

class Registry {
public:
    Counter& counter(const std::string& name, const std::string& help,
                     const std::map<std::string, std::string>& labels = {}) {
        std::lock_guard<std::mutex> lock(mu_);
        auto key = name + format_labels(labels);
        auto it = counters_.find(key);
        if (it != counters_.end()) return *it->second;
        auto c = std::make_unique<Counter>();
        auto* ptr = c.get();
        counters_[key] = std::move(c);
        meta_.push_back({name, help, "counter", format_labels(labels)});
        return *ptr;
    }

    Gauge& gauge(const std::string& name, const std::string& help,
                 const std::map<std::string, std::string>& labels = {}) {
        std::lock_guard<std::mutex> lock(mu_);
        auto key = name + format_labels(labels);
        auto it = gauges_.find(key);
        if (it != gauges_.end()) return *it->second;
        auto g = std::make_unique<Gauge>();
        auto* ptr = g.get();
        gauges_[key] = std::move(g);
        meta_.push_back({name, help, "gauge", format_labels(labels)});
        return *ptr;
    }

    Histogram& histogram(const std::string& name, const std::string& help,
                         const std::vector<double>& buckets = Histogram::default_buckets(),
                         const std::map<std::string, std::string>& labels = {}) {
        std::lock_guard<std::mutex> lock(mu_);
        auto key = name + format_labels(labels);
        auto it = histograms_.find(key);
        if (it != histograms_.end()) return *it->second;
        auto h = std::make_unique<Histogram>(buckets);
        auto* ptr = h.get();
        histograms_[key] = std::move(h);
        meta_.push_back({name, help, "histogram", format_labels(labels)});
        return *ptr;
    }

    std::string render() const {
        std::lock_guard<std::mutex> lock(mu_);
        std::ostringstream out;
        std::set<std::string> emitted_types;

        for (const auto& [key, c] : counters_) {
            auto& m = find_meta(key);
            emit_type_help(out, m.name, "counter", m.help, emitted_types);
            out << m.name << m.labels << " " << c->value() << "\n";
        }
        for (const auto& [key, g] : gauges_) {
            auto& m = find_meta(key);
            emit_type_help(out, m.name, "gauge", m.help, emitted_types);
            out << m.name << m.labels << " " << g->value() << "\n";
        }
        for (const auto& [key, h] : histograms_) {
            auto& m = find_meta(key);
            emit_type_help(out, m.name, "histogram", m.help, emitted_types);
            uint64_t cumulative = 0;
            for (size_t i = 0; i < h->bounds().size(); ++i) {
                cumulative += h->bucket_count(i);
                out << m.name << "_bucket{le=\"" << h->bounds()[i] << "\""
                    << strip_braces(m.labels) << "} " << cumulative << "\n";
            }
            cumulative += h->bucket_count(h->bounds().size());
            out << m.name << "_bucket{le=\"+Inf\"" << strip_braces(m.labels) << "} " << cumulative << "\n";
            out << m.name << "_sum" << m.labels << " " << h->sum() << "\n";
            out << m.name << "_count" << m.labels << " " << h->count() << "\n";
        }
        return out.str();
    }

private:
    static std::string format_labels(const std::map<std::string, std::string>& labels) {
        if (labels.empty()) return "";
        std::ostringstream out;
        out << "{";
        bool first = true;
        for (const auto& [k, v] : labels) {
            if (!first) out << ",";
            out << k << "=\"" << v << "\"";
            first = false;
        }
        out << "}";
        return out.str();
    }

    static std::string strip_braces(const std::string& labels) {
        if (labels.empty()) return "";
        return "," + labels.substr(1, labels.size() - 2);
    }

    static void emit_type_help(std::ostringstream& out, const std::string& name,
                               const std::string& type, const std::string& help,
                               std::set<std::string>& emitted) {
        if (emitted.count(name)) return;
        emitted.insert(name);
        out << "# HELP " << name << " " << help << "\n";
        out << "# TYPE " << name << " " << type << "\n";
    }

    const MetricMeta& find_meta(const std::string& key) const {
        for (const auto& m : meta_) {
            if (key == m.name + m.labels) return m;
        }
        return meta_.back();
    }

    mutable std::mutex mu_;
    std::map<std::string, std::unique_ptr<Counter>> counters_;
    std::map<std::string, std::unique_ptr<Gauge>> gauges_;
    std::map<std::string, std::unique_ptr<Histogram>> histograms_;
    std::vector<MetricMeta> meta_;
};

class ScopedTimer {
public:
    ScopedTimer(Histogram& h) : hist_(h), start_(std::chrono::steady_clock::now()) {}
    ~ScopedTimer() {
        auto elapsed = std::chrono::steady_clock::now() - start_;
        double seconds = std::chrono::duration<double>(elapsed).count();
        hist_.observe(seconds);
    }
private:
    Histogram& hist_;
    std::chrono::steady_clock::time_point start_;
};

}} // namespace nanodb::metrics
