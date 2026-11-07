// A small JSON value, reader and writer: configs, LoadFrom state files, results.json.
#pragma once
#include <cstdint>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace cc {

struct Json {
    enum Kind { Null, Bool, Int, UInt, Float, String, Array, Object };
    Kind kind = Null;
    bool b = false;
    int64_t i = 0;        // Int
    uint64_t u = 0;       // UInt: an integer above INT64_MAX (a 64-bit seed)
    double d = 0;         // Float
    std::string s;
    std::vector<Json> arr;
    std::vector<std::pair<std::string, Json>> obj;   // in the order of the text

    bool is_null() const { return kind == Null; }
    bool is_number() const { return kind == Int || kind == UInt || kind == Float; }
    const Json* get(const std::string& key) const;    // nullptr when absent (or not an object)
    const Json& at(const std::string& key) const;     // throws when absent
    int64_t as_int() const;                            // integers, and floats truncated as Python's int()
    uint64_t as_u64() const;
    double as_double() const;
    bool as_bool() const;
    const std::string& as_string() const;

    static Json null() { return Json(); }
    static Json boolean(bool v) { Json j; j.kind = Bool; j.b = v; return j; }
    static Json integer(int64_t v) { Json j; j.kind = Int; j.i = v; return j; }
    static Json unsigned_integer(uint64_t v);
    static Json string(std::string v) { Json j; j.kind = String; j.s = std::move(v); return j; }
    static Json array() { Json j; j.kind = Array; return j; }
    static Json object() { Json j; j.kind = Object; return j; }
    Json& add(const std::string& key, Json v) { obj.emplace_back(key, std::move(v)); return *this; }
    Json& push(Json v) { arr.push_back(std::move(v)); return *this; }
};

struct JsonError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

Json parse_json(const std::string& text);
// As Python's json.dumps(value, indent=2): non-ASCII escaped, no final newline.
std::string dump_json(const Json& value, int indent = 2);

}  // namespace cc
