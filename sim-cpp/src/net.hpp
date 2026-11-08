// What the runner asks of the system, the same on Linux, macOS and Windows: TCP sockets (POSIX sockets, or Winsock 2
// on Windows) and the browser it opens. net.cpp is the only file that includes the system's headers: windows.h
// defines macros (min, max, ERROR, near, far, DrawText...) that the rest of the code must not see.
#pragma once
#include <cstdint>
#include <optional>
#include <string>

namespace cc::net {

#ifdef _WIN32
using socket_t = std::uintptr_t;      // SOCKET
constexpr socket_t INVALID = ~socket_t(0);
#else
using socket_t = int;
constexpr socket_t INVALID = -1;
#endif

// A socket listening on host:port (an address or a name; port 0: any free one), with `backlog`; throws a
// std::runtime_error ("127.0.0.1:31001: Address already in use") when it cannot be had.
socket_t listen_tcp(const std::string& host, int port, int backlog);
int local_port(socket_t s);           // the port a socket is bound to

// A connection from a listening socket, or INVALID (last_error() says why). Neither it nor a listening socket is
// inherited by the processes the runner starts (the browser), and a peer that hangs up never ends the runner
// (SIGPIPE on Linux and macOS).
socket_t accept(socket_t listener);
void close(socket_t s);
// Waits until s has something to read (or to accept): 1, 0 after timeout_ms, -1 on an error (last_error()).
int wait_readable(socket_t s, int timeout_ms);
long send(socket_t s, const char* data, size_t n);      // -1 on an error (last_error())
long recv(socket_t s, char* buf, size_t n);             // 0 when the peer has closed, -1 on an error
// Both directions; a recv waiting on s in another thread returns.
void shutdown(socket_t s);
// How long a recv or send on s may wait (SO_RCVTIMEO, SO_SNDTIMEO); none: as long as it takes.
void set_timeout(socket_t s, std::optional<double> seconds);
void set_nodelay(socket_t s);

int last_error();                     // errno, or WSAGetLastError() on Windows
bool interrupted(int error);          // a signal stopped the call: try again (never on Windows)
bool timed_out(int error);            // the timeout of set_timeout ran out (EAGAIN, WSAETIMEDOUT)
std::string error_text(int error);    // the system's words for it, in English where it has them

// Opens a URL in the user's browser, in the background; with no browser at hand nothing happens.
void open_browser(const std::string& url);

}  // namespace cc::net
