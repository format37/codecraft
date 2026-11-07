// A compact JSON writer for the viewer's event stream (one line per event, no spaces).
#pragma once
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <string>

namespace cc {

struct Out {
    std::string s;
    Out& raw(const char* x) {
        s += x;
        return *this;
    }
    Out& raw(char c) {
        s += c;
        return *this;
    }
    Out& num(int64_t v) {
        s += std::to_string(v);
        return *this;
    }
    Out& flt(double v) {
        if (!std::isfinite(v)) return raw("0");
        char b[32];
        snprintf(b, sizeof b, "%.6g", v);
        s += b;
        return *this;
    }
    Out& boolean(bool v) { return raw(v ? "true" : "false"); }
    Out& str(const std::string& v) {
        s += '"';
        for (unsigned char c : v) {
            if (c == '"') s += "\\\"";
            else if (c == '\\') s += "\\\\";
            else if (c == '\n') s += "\\n";
            else if (c == '\r') s += "\\r";
            else if (c == '\t') s += "\\t";
            else if (c < 0x20) {
                char b[8];
                snprintf(b, sizeof b, "\\u%04x", c);
                s += b;
            } else {
                s += static_cast<char>(c);
            }
        }
        s += '"';
        return *this;
    }
    Out& key(const char* k) {               // "k": (after a comma unless the object was just opened)
        if (!s.empty() && s.back() != '{') s += ',';
        s += '"';
        s += k;
        s += "\":";
        return *this;
    }
    Out& sep() {                             // a comma between array items
        if (!s.empty() && s.back() != '[') s += ',';
        return *this;
    }
};

}  // namespace cc
