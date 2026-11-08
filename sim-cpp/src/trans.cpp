#include "trans.hpp"

#include <cerrno>
#include <cstring>
#include <sys/socket.h>

namespace cc {

std::string os_error(int err) {
    if (err == EAGAIN || err == EWOULDBLOCK) return "IO error: Resource temporarily unavailable (os error 11)";
    return std::string("IO error: ") + strerror(err) + " (os error " + std::to_string(err) + ")";
}

void no_sigpipe(int fd) {
#ifdef SO_NOSIGPIPE
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#else
    (void)fd;
#endif
}

void SocketReader::read(void* out, size_t n) {
    char* dst = static_cast<char*>(out);
    while (n > 0) {
        if (pos_ == len_) {
            ssize_t got;
            do {
                got = recv(fd_, buf_, sizeof buf_, 0);
            } while (got < 0 && errno == EINTR);
            if (got == 0) throw IoError("IO error: failed to fill whole buffer");
            if (got < 0) throw IoError(os_error(errno));
            pos_ = 0;
            len_ = static_cast<size_t>(got);
        }
        size_t k = std::min(n, len_ - pos_);
        memcpy(dst, buf_ + pos_, k);
        pos_ += k;
        dst += k;
        n -= k;
    }
}

int32_t SocketReader::i32() {
    unsigned char b[4];
    read(b, 4);
    return static_cast<int32_t>(static_cast<uint32_t>(b[0]) | (static_cast<uint32_t>(b[1]) << 8) |
                                (static_cast<uint32_t>(b[2]) << 16) | (static_cast<uint32_t>(b[3]) << 24));
}

float SocketReader::f32() {
    int32_t v = i32();
    float f;
    memcpy(&f, &v, 4);
    return f;
}

bool SocketReader::boolean() {
    unsigned char b;
    read(&b, 1);
    if (b > 1) throw IoError("IO error: bad bool " + std::to_string(b));
    return b == 1;
}

int32_t SocketReader::count() {
    int32_t n = i32();
    if (n < 0 || n > 10000000) throw IoError("IO error: bad count " + std::to_string(n));
    return n;
}

std::string SocketReader::string() {
    int32_t n = count();
    std::string s(static_cast<size_t>(n), '\0');
    if (n) read(&s[0], static_cast<size_t>(n));
    return s;
}

void write_vec2(Writer& w, Vec2 v) {
    w.i32(v.x);
    w.i32(v.y);
}

static void write_types(Writer& w, const std::vector<EntityType>& types) {
    w.i32(static_cast<int32_t>(types.size()));
    for (EntityType t : types) w.i32(static_cast<int32_t>(t));
}

static void write_properties(Writer& w, const EntityProperties& p) {
    w.i32(p.size);
    w.i32(p.build_score);
    w.i32(p.destroy_score);
    w.boolean(p.can_move);
    w.i32(p.population_provide);
    w.i32(p.population_use);
    w.i32(p.max_health);
    w.i32(p.initial_cost);
    w.i32(p.sight_range);
    w.i32(p.resource_per_health);
    w.boolean(p.build.has_value());
    if (p.build) {
        write_types(w, p.build->options);
        w.boolean(p.build->init_health.has_value());
        if (p.build->init_health) w.i32(*p.build->init_health);
    }
    w.boolean(p.attack.has_value());
    if (p.attack) {
        w.i32(p.attack->attack_range);
        w.i32(p.attack->damage);
        w.boolean(p.attack->collect_resource);
    }
    w.boolean(p.repair.has_value());
    if (p.repair) {
        write_types(w, p.repair->valid_targets);
        w.i32(p.repair->power);
    }
}

void write_properties_map(Writer& w, const Properties& props) {
    w.i32(static_cast<int32_t>(props.list.size()));
    for (const auto& tp : props.list) {
        w.i32(static_cast<int32_t>(tp.first));
        write_properties(w, tp.second);
    }
}

void write_entity(Writer& w, const Entity& e) {
    w.i32(e.id);
    w.boolean(e.player_id.has_value());
    if (e.player_id) w.i32(*e.player_id);
    w.i32(static_cast<int32_t>(e.entity_type));
    write_vec2(w, e.position);
    w.i32(e.health);
    w.boolean(e.active);
}

void write_entity_action(Writer& w, const EntityAction& a) {
    w.boolean(a.move_action.has_value());
    if (a.move_action) {
        write_vec2(w, a.move_action->target);
        w.boolean(a.move_action->find_closest_position);
        w.boolean(a.move_action->break_through);
    }
    w.boolean(a.build_action.has_value());
    if (a.build_action) {
        w.i32(static_cast<int32_t>(a.build_action->entity_type));
        write_vec2(w, a.build_action->position);
    }
    w.boolean(a.attack_action.has_value());
    if (a.attack_action) {
        w.boolean(a.attack_action->target.has_value());
        if (a.attack_action->target) w.i32(*a.attack_action->target);
        w.boolean(a.attack_action->auto_attack.has_value());
        if (a.attack_action->auto_attack) {
            w.i32(a.attack_action->auto_attack->pathfind_range);
            write_types(w, a.attack_action->auto_attack->valid_targets);
        }
    }
    w.boolean(a.repair_action.has_value());
    if (a.repair_action) w.i32(a.repair_action->target);
}

void write_player_view(Writer& w, const PlayerView& v) {
    w.i32(v.my_id);
    w.i32(v.map_size);
    w.boolean(v.fog_of_war);
    write_properties_map(w, *v.props);
    w.i32(v.max_tick_count);
    w.i32(v.max_pathfind_nodes);
    w.i32(v.current_tick);
    w.i32(static_cast<int32_t>(v.players.size()));
    for (const ViewPlayer& p : v.players) {
        w.i32(p.id);
        w.i32(p.score);
        w.i32(p.resource);
    }
    w.i32(static_cast<int32_t>(v.entities.size()));
    for (const Entity& e : v.entities) write_entity(w, e);
}

void write_get_action(Writer& w, const PlayerView& v, bool debug_available) {
    w.i32(0);
    write_player_view(w, v);
    w.boolean(debug_available);
}

void write_finish(Writer& w) { w.i32(1); }

void write_debug_update(Writer& w, const PlayerView& v) {
    w.i32(2);
    write_player_view(w, v);
}

static EntityType read_type(SocketReader& r) {
    int32_t t = r.i32();
    if (t < 0 || t >= TYPE_COUNT) throw IoError("IO error: bad entity type " + std::to_string(t));
    return static_cast<EntityType>(t);
}

static std::vector<EntityType> read_types(SocketReader& r) {
    std::vector<EntityType> out;
    int32_t n = r.count();
    for (int32_t k = 0; k < n; k++) out.push_back(read_type(r));
    return out;
}

static Vec2 read_vec2(SocketReader& r) {
    int32_t x = r.i32();
    return Vec2{x, r.i32()};
}

static EntityAction read_entity_action(SocketReader& r) {
    EntityAction a;
    if (r.boolean()) {
        Vec2 t = read_vec2(r);
        bool fc = r.boolean();
        a.move_action = MoveAction{t, fc, r.boolean()};
    }
    if (r.boolean()) {
        EntityType t = read_type(r);
        a.build_action = BuildAction{t, read_vec2(r)};
    }
    if (r.boolean()) {
        AttackAction at;
        if (r.boolean()) at.target = r.i32();
        if (r.boolean()) {
            int32_t range = r.i32();
            at.auto_attack = AutoAttack{range, read_types(r)};
        }
        a.attack_action = at;
    }
    if (r.boolean()) a.repair_action = RepairAction{r.i32()};
    return a;
}

Action read_action(SocketReader& r) {
    // a map: a repeated id keeps its first place and takes the last value, as a Python dict
    Action out;
    int32_t n = r.count();
    for (int32_t k = 0; k < n; k++) {
        int32_t id = r.i32();
        EntityAction a = read_entity_action(r);
        bool found = false;
        for (auto& kv : out)
            if (kv.first == id) {
                kv.second = a;
                found = true;
                break;
            }
        if (!found) out.emplace_back(id, std::move(a));
    }
    return out;
}

}  // namespace cc
