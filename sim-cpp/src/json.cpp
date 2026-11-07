#include "json.hpp"

#include <cctype>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>

namespace cc {

Json Json::unsigned_integer(uint64_t v) {
    Json j;
    if (v <= static_cast<uint64_t>(INT64_MAX)) {
        j.kind = Int;
        j.i = static_cast<int64_t>(v);
    } else {
        j.kind = UInt;
        j.u = v;
    }
    return j;
}

const Json* Json::get(const std::string& key) const {
    if (kind != Object) return nullptr;
    for (const auto& kv : obj)
        if (kv.first == key) return &kv.second;
    return nullptr;
}

const Json& Json::at(const std::string& key) const {
    const Json* v = get(key);
    if (!v) throw JsonError("missing field `" + key + "`");
    return *v;
}

int64_t Json::as_int() const {
    switch (kind) {
        case Int: return i;
        case UInt: return static_cast<int64_t>(u);
        case Float: return static_cast<int64_t>(std::trunc(d));
        case Bool: return b ? 1 : 0;
        default: throw JsonError("an integer was expected");
    }
}

uint64_t Json::as_u64() const {
    if (kind == UInt) return u;
    if (kind == Int && i >= 0) return static_cast<uint64_t>(i);
    throw JsonError("an unsigned 64-bit integer was expected");
}

double Json::as_double() const {
    switch (kind) {
        case Int: return static_cast<double>(i);
        case UInt: return static_cast<double>(u);
        case Float: return d;
        default: throw JsonError("a number was expected");
    }
}

bool Json::as_bool() const {
    switch (kind) {
        case Bool: return b;
        case Int: return i != 0;
        case UInt: return u != 0;
        case Float: return d != 0;
        case Null: return false;
        case String: return !s.empty();
        case Array: return !arr.empty();
        case Object: return !obj.empty();
    }
    return false;
}

const std::string& Json::as_string() const {
    if (kind != String) throw JsonError("a string was expected");
    return s;
}

namespace {

struct Parser {
    const std::string& t;
    size_t p = 0;

    [[noreturn]] void fail(const std::string& what) const {
        throw JsonError(what + " at character " + std::to_string(p));
    }
    void ws() {
        while (p < t.size() && (t[p] == ' ' || t[p] == '\t' || t[p] == '\n' || t[p] == '\r')) p++;
    }
    bool take(const char* word) {
        size_t n = std::char_traits<char>::length(word);
        if (t.compare(p, n, word) == 0) {
            p += n;
            return true;
        }
        return false;
    }
    void put_utf8(std::string& out, uint32_t c) {
        if (c < 0x80) {
            out += static_cast<char>(c);
        } else if (c < 0x800) {
            out += static_cast<char>(0xC0 | (c >> 6));
            out += static_cast<char>(0x80 | (c & 0x3F));
        } else if (c < 0x10000) {
            out += static_cast<char>(0xE0 | (c >> 12));
            out += static_cast<char>(0x80 | ((c >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (c & 0x3F));
        } else {
            out += static_cast<char>(0xF0 | (c >> 18));
            out += static_cast<char>(0x80 | ((c >> 12) & 0x3F));
            out += static_cast<char>(0x80 | ((c >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (c & 0x3F));
        }
    }
    uint32_t hex4() {
        if (p + 4 > t.size()) fail("bad \\u escape");
        uint32_t v = 0;
        for (int k = 0; k < 4; k++) {
            char c = t[p++];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= c - '0';
            else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
            else fail("bad \\u escape");
        }
        return v;
    }
    std::string str() {
        if (t[p] != '"') fail("a string was expected");
        p++;
        std::string out;
        while (true) {
            if (p >= t.size()) fail("unterminated string");
            char c = t[p++];
            if (c == '"') break;
            if (c != '\\') {
                out += c;
                continue;
            }
            if (p >= t.size()) fail("unterminated string");
            char e = t[p++];
            switch (e) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'n': out += '\n'; break;
                case 'r': out += '\r'; break;
                case 't': out += '\t'; break;
                case 'u': {
                    uint32_t c1 = hex4();
                    if (c1 >= 0xD800 && c1 < 0xDC00 && t.compare(p, 2, "\\u") == 0) {
                        p += 2;
                        uint32_t c2 = hex4();
                        c1 = 0x10000 + ((c1 - 0xD800) << 10) + (c2 - 0xDC00);
                    }
                    put_utf8(out, c1);
                    break;
                }
                default: fail("bad escape");
            }
        }
        return out;
    }
    Json number() {
        size_t start = p;
        bool neg = false, integral = true;
        if (t[p] == '-') {
            neg = true;
            p++;
        }
        while (p < t.size() && isdigit(static_cast<unsigned char>(t[p]))) p++;
        if (p < t.size() && t[p] == '.') {
            integral = false;
            p++;
            while (p < t.size() && isdigit(static_cast<unsigned char>(t[p]))) p++;
        }
        if (p < t.size() && (t[p] == 'e' || t[p] == 'E')) {
            integral = false;
            p++;
            if (p < t.size() && (t[p] == '+' || t[p] == '-')) p++;
            while (p < t.size() && isdigit(static_cast<unsigned char>(t[p]))) p++;
        }
        std::string text = t.substr(start, p - start);
        if (text == "-" || text.empty()) fail("bad number");
        if (integral) {
            errno = 0;
            if (neg) {
                long long v = strtoll(text.c_str(), nullptr, 10);
                if (errno == 0) return Json::integer(v);
            } else {
                unsigned long long v = strtoull(text.c_str(), nullptr, 10);
                if (errno == 0) return Json::unsigned_integer(v);
            }
        }
        Json j;
        j.kind = Json::Float;
        j.d = strtod(text.c_str(), nullptr);
        return j;
    }
    Json value() {
        ws();
        if (p >= t.size()) fail("unexpected end");
        char c = t[p];
        if (c == '{') {
            p++;
            Json j = Json::object();
            ws();
            if (p < t.size() && t[p] == '}') {
                p++;
                return j;
            }
            while (true) {
                ws();
                std::string k = str();
                ws();
                if (p >= t.size() || t[p] != ':') fail("':' was expected");
                p++;
                Json v = value();
                bool replaced = false;
                for (auto& kv : j.obj)
                    if (kv.first == k) {     // a repeated key: the last value, as Python's json
                        kv.second = std::move(v);
                        replaced = true;
                        break;
                    }
                if (!replaced) j.obj.emplace_back(std::move(k), std::move(v));
                ws();
                if (p < t.size() && t[p] == ',') {
                    p++;
                    continue;
                }
                if (p < t.size() && t[p] == '}') {
                    p++;
                    return j;
                }
                fail("',' or '}' was expected");
            }
        }
        if (c == '[') {
            p++;
            Json j = Json::array();
            ws();
            if (p < t.size() && t[p] == ']') {
                p++;
                return j;
            }
            while (true) {
                j.arr.push_back(value());
                ws();
                if (p < t.size() && t[p] == ',') {
                    p++;
                    continue;
                }
                if (p < t.size() && t[p] == ']') {
                    p++;
                    return j;
                }
                fail("',' or ']' was expected");
            }
        }
        if (c == '"') return Json::string(str());
        if (take("true")) return Json::boolean(true);
        if (take("false")) return Json::boolean(false);
        if (take("null")) return Json::null();
        if (c == '-' || isdigit(static_cast<unsigned char>(c))) return number();
        fail("a value was expected");
    }
};

void escape(std::string& out, const std::string& s) {
    out += '"';
    for (size_t k = 0; k < s.size();) {
        unsigned char c = static_cast<unsigned char>(s[k]);
        uint32_t cp;
        int len = 1;
        if (c < 0x80) cp = c;
        else if ((c >> 5) == 6 && k + 1 < s.size()) { cp = ((c & 0x1F) << 6) | (s[k + 1] & 0x3F); len = 2; }
        else if ((c >> 4) == 14 && k + 2 < s.size()) {
            cp = ((c & 0x0F) << 12) | ((s[k + 1] & 0x3F) << 6) | (s[k + 2] & 0x3F);
            len = 3;
        } else if ((c >> 3) == 30 && k + 3 < s.size()) {
            cp = ((c & 0x07) << 18) | ((s[k + 1] & 0x3F) << 12) | ((s[k + 2] & 0x3F) << 6) | (s[k + 3] & 0x3F);
            len = 4;
        } else cp = 0xFFFD;
        k += len;
        char buf[16];
        switch (cp) {
            case '"': out += "\\\""; continue;
            case '\\': out += "\\\\"; continue;
            case '\n': out += "\\n"; continue;
            case '\r': out += "\\r"; continue;
            case '\t': out += "\\t"; continue;
            case '\b': out += "\\b"; continue;
            case '\f': out += "\\f"; continue;
            default: break;
        }
        if (cp >= 0x20 && cp < 0x7F) {
            out += static_cast<char>(cp);
        } else if (cp >= 0x10000) {
            uint32_t v = cp - 0x10000;
            snprintf(buf, sizeof buf, "\\u%04x\\u%04x", 0xD800 + (v >> 10), 0xDC00 + (v & 0x3FF));
            out += buf;
        } else {
            snprintf(buf, sizeof buf, "\\u%04x", cp);
            out += buf;
        }
    }
    out += '"';
}

void dump(std::string& out, const Json& v, int indent, int level) {
    auto newline = [&](int lv) {
        out += '\n';
        out.append(static_cast<size_t>(indent * lv), ' ');
    };
    switch (v.kind) {
        case Json::Null: out += "null"; break;
        case Json::Bool: out += v.b ? "true" : "false"; break;
        case Json::Int: out += std::to_string(v.i); break;
        case Json::UInt: out += std::to_string(v.u); break;
        case Json::Float: {
            char buf[32];
            snprintf(buf, sizeof buf, "%.17g", v.d);
            out += buf;
            break;
        }
        case Json::String: escape(out, v.s); break;
        case Json::Array:
            if (v.arr.empty()) {
                out += "[]";
                break;
            }
            out += '[';
            for (size_t k = 0; k < v.arr.size(); k++) {
                if (k) out += ',';
                newline(level + 1);
                dump(out, v.arr[k], indent, level + 1);
            }
            newline(level);
            out += ']';
            break;
        case Json::Object:
            if (v.obj.empty()) {
                out += "{}";
                break;
            }
            out += '{';
            for (size_t k = 0; k < v.obj.size(); k++) {
                if (k) out += ',';
                newline(level + 1);
                escape(out, v.obj[k].first);
                out += ": ";
                dump(out, v.obj[k].second, indent, level + 1);
            }
            newline(level);
            out += '}';
            break;
    }
}

}  // namespace

Json parse_json(const std::string& text) {
    Parser ps{text};
    Json v = ps.value();
    ps.ws();
    if (ps.p != text.size()) ps.fail("extra data");
    return v;
}

std::string dump_json(const Json& value, int indent) {
    std::string out;
    dump(out, value, indent, 0);
    return out;
}

}  // namespace cc
