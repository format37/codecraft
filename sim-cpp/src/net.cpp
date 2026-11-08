// Windows: WSAPoll and CancelIoEx came with Vista, and older MinGW-w64 headers assume XP unless told otherwise
// before the first header (any of them includes _mingw.h, which settles the version).
#if defined(_WIN32) && (!defined(_WIN32_WINNT) || _WIN32_WINNT < 0x0600)
#undef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include "net.hpp"

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <objbase.h>
#include <shellapi.h>
#include <type_traits>
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0  // macOS has no such flag: accept() sets SO_NOSIGPIPE on the socket instead
#endif
#endif

#include <algorithm>
#include <cerrno>
#include <climits>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include <stdexcept>
#include <thread>

namespace cc::net {

#ifdef _WIN32
static_assert(std::is_same<SOCKET, socket_t>::value, "socket_t is SOCKET");
static_assert(INVALID == INVALID_SOCKET, "INVALID is INVALID_SOCKET");
#endif

namespace {

#ifdef _WIN32
// Winsock wants WSAStartup before any other call, getaddrinfo included: once, before main.
struct Winsock {
    Winsock() {
        WSADATA data;
        WSAStartup(MAKEWORD(2, 2), &data);
    }
} winsock;
#endif

// Not inherited by the processes the runner starts (the browser, through xdg-open or ShellExecute): a browser
// started that way would otherwise hold the viewer's port after the runner has quit.
void no_inherit(socket_t s) {
#ifdef _WIN32
    SetHandleInformation(reinterpret_cast<HANDLE>(s), HANDLE_FLAG_INHERIT, 0);
#else
    fcntl(s, F_SETFD, FD_CLOEXEC);
#endif
}

void set_flag(socket_t s, int level, int name) {
    int one = 1;
    setsockopt(s, level, name, reinterpret_cast<const char*>(&one), sizeof one);
}

}  // namespace

socket_t listen_tcp(const std::string& host, int port, int backlog) {
    addrinfo hints{}, *res = nullptr;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_flags = AI_PASSIVE;
    const std::string service = std::to_string(port);
    if (getaddrinfo(host.c_str(), service.c_str(), &hints, &res) != 0 || !res)
        throw std::runtime_error("cannot resolve " + host);
    socket_t s = ::socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    bool ok = s != INVALID;
    if (ok) {
        no_inherit(s);
#ifndef _WIN32
        // a port that the last game's connection has just left (TIME_WAIT) can be listened on again at once; Windows
        // allows that as it is, and there SO_REUSEADDR would let this socket take a port that another one listens on
        set_flag(s, SOL_SOCKET, SO_REUSEADDR);
#endif
        ok = ::bind(s, res->ai_addr, static_cast<socklen_t>(res->ai_addrlen)) == 0 && ::listen(s, backlog) == 0;
    }
    const int error = ok ? 0 : last_error();
    freeaddrinfo(res);
    if (!ok) {
        if (s != INVALID) close(s);
        throw std::runtime_error(host + ":" + service + ": " + error_text(error));
    }
    return s;
}

int local_port(socket_t s) {
    sockaddr_in addr{};
    socklen_t len = sizeof addr;
    if (getsockname(s, reinterpret_cast<sockaddr*>(&addr), &len) != 0) return 0;
    return ntohs(addr.sin_port);
}

socket_t accept(socket_t listener) {
    socket_t s = ::accept(listener, nullptr, nullptr);
    if (s == INVALID) return s;
    no_inherit(s);
#ifdef SO_NOSIGPIPE
    set_flag(s, SOL_SOCKET, SO_NOSIGPIPE);      // macOS: its send() has no MSG_NOSIGNAL
#endif
    return s;
}

void close(socket_t s) {
#ifdef _WIN32
    ::closesocket(s);
#else
    ::close(s);
#endif
}

int wait_readable(socket_t s, int timeout_ms) {
#ifdef _WIN32
    WSAPOLLFD p{};
    p.fd = s;
    p.events = POLLIN;
    return WSAPoll(&p, 1, timeout_ms);
#else
    pollfd p{s, POLLIN, 0};
    return ::poll(&p, 1, timeout_ms);
#endif
}

long send(socket_t s, const char* data, size_t n) {
#ifdef _WIN32
    return ::send(s, data, static_cast<int>(std::min<size_t>(n, INT_MAX)), 0);
#else
    return static_cast<long>(::send(s, data, n, MSG_NOSIGNAL));
#endif
}

long recv(socket_t s, char* buf, size_t n) {
#ifdef _WIN32
    return ::recv(s, buf, static_cast<int>(std::min<size_t>(n, INT_MAX)), 0);
#else
    return static_cast<long>(::recv(s, buf, n, 0));
#endif
}

void shutdown(socket_t s) {
#ifdef _WIN32
    ::shutdown(s, SD_BOTH);
    // a blocking recv of another thread may go on waiting after a shutdown on Windows: cancel it
    CancelIoEx(reinterpret_cast<HANDLE>(s), nullptr);
#else
    ::shutdown(s, SHUT_RDWR);
#endif
}

void set_timeout(socket_t s, std::optional<double> seconds) {
#ifdef _WIN32
    // milliseconds in a DWORD (0: none), rounded up: a limit is never cut short
    DWORD ms = 0;
    if (seconds) ms = static_cast<DWORD>(std::clamp(std::ceil(*seconds * 1000), 1.0, 2147483647.0));
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&ms), sizeof ms);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&ms), sizeof ms);
#else
    timeval tv{0, 0};
    if (seconds) {
        double v = *seconds;
        tv.tv_sec = static_cast<time_t>(v);
        tv.tv_usec = static_cast<suseconds_t>((v - static_cast<double>(tv.tv_sec)) * 1e6);
        if (tv.tv_sec == 0 && tv.tv_usec == 0) tv.tv_usec = 1;
    }
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
#endif
}

void set_nodelay(socket_t s) { set_flag(s, IPPROTO_TCP, TCP_NODELAY); }

int last_error() {
#ifdef _WIN32
    return WSAGetLastError();
#else
    return errno;
#endif
}

bool interrupted(int error) {
#ifdef _WIN32
    (void)error;
    return false;      // no signals; WSAEINTR is a call cancelled on purpose (shutdown above), not one to repeat
#else
    return error == EINTR;
#endif
}

bool timed_out(int error) {
#ifdef _WIN32
    return error == WSAETIMEDOUT || error == WSAEWOULDBLOCK || error == EAGAIN;
#else
    return error == EAGAIN || error == EWOULDBLOCK;
#endif
}

std::string error_text(int error) {
#ifdef _WIN32
    // FormatMessage, in English when Windows has it in English (the log is plain ASCII then), as UTF-8 either way
    const DWORD flags = FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS;
    wchar_t* text = nullptr;
    DWORD n = 0;
    for (DWORD lang : {static_cast<DWORD>(MAKELANGID(LANG_ENGLISH, SUBLANG_ENGLISH_US)), static_cast<DWORD>(0)}) {
        n = FormatMessageW(flags, nullptr, static_cast<DWORD>(error), lang, reinterpret_cast<wchar_t*>(&text), 0,
                           nullptr);
        if (n) break;
    }
    std::string out;
    if (n) {
        int len = WideCharToMultiByte(CP_UTF8, 0, text, static_cast<int>(n), nullptr, 0, nullptr, nullptr);
        if (len > 0) {
            out.resize(static_cast<size_t>(len));
            WideCharToMultiByte(CP_UTF8, 0, text, static_cast<int>(n), &out[0], len, nullptr, nullptr);
        }
    }
    if (text) LocalFree(text);
    while (!out.empty() && (out.back() == '\r' || out.back() == '\n' || out.back() == ' ' || out.back() == '.'))
        out.pop_back();
    return out.empty() ? "error " + std::to_string(error) : out;
#else
    return strerror(error);
#endif
}

void open_browser(const std::string& url) {
#ifdef _WIN32
    // as the shell opens any document, with no console window flashing; COM first, as ShellExecute asks
    std::thread([url] {
        HRESULT com = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
        ShellExecuteA(nullptr, "open", url.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
        if (SUCCEEDED(com)) CoUninitialize();
    }).detach();
#else
#ifdef __APPLE__
    std::string cmd = "open '" + url + "' >/dev/null 2>&1";
#else
    std::string cmd = "xdg-open '" + url + "' >/dev/null 2>&1";
#endif
    std::thread([cmd] {
        if (std::system(cmd.c_str()) != 0) {
        }
    }).detach();
#endif
}

}  // namespace cc::net
