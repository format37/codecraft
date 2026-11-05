"""The game runner's isolation (security review, findings 4, 5 and 7) without Docker: the seat networks, the
engine's per-seat listen addresses, the containers' settings and the public copy of the engine config."""
import ipaddress
from unittest.mock import MagicMock

import docker
import pytest

from codecraft.config import Config
from codecraft.game import GameRunner, PlayerSpec, Seat, redacted, seat_subnet
from codecraft.strategies import Strategy


def runner(tmp_path):
    cfg = Config(Config.load(tmp_path).data, tmp_path)
    client = MagicMock()
    client.api.create_endpoint_config.side_effect = lambda ipv4_address: {"IPAMConfig": {"IPv4Address": ipv4_address}}
    return GameRunner(cfg, client, db=None), client


def tcp_seat(slot: int, engine_ip: str | None = None) -> Seat:
    seat = Seat(slot=slot, spec=PlayerSpec(Strategy(name=f"bot{slot}", language="python"), f"img{slot}"),
                port=31001 + slot, token=f"tok{slot}", engine_ip=engine_ip)
    seat.limits = {"memory_mb": 1024, "cpus": 1.0, "pids": 256, "cpu_time_sec": 40, "accept_timeout_sec": 60,
                   "tick_timeout_sec": 1.0}
    return seat


def test_every_seat_gets_its_own_subnet_and_a_retry_moves_by_64():
    base = "10.200.0.0/16"
    nets = [seat_subnet(base, 52, slot) for slot in range(4)]
    assert [str(n) for n in nets] == ["10.200.208.0/24", "10.200.209.0/24", "10.200.210.0/24", "10.200.211.0/24"]
    assert str(seat_subnet(base, 52, 1, attempt=1)) == "10.200.17.0/24"          # (209 + 64) % 256
    assert seat_subnet(base, 52 + 64, 0) == seat_subnet(base, 52, 0)             # 64 games apart: the same /24
    assert all(n.subnet_of(ipaddress.ip_network(base)) for n in nets)


def test_the_engine_listens_for_a_seat_on_that_seats_network_only(tmp_path):
    gr, _ = runner(tmp_path)
    quick = Seat(slot=1, spec=PlayerSpec(Strategy(name="quickstart", builtin="QuickStart")))
    empty = Seat(slot=2, spec=PlayerSpec(None))
    config = gr._engine_config("Round1", [tcp_seat(0, "10.200.208.2"), quick, empty, tcp_seat(3, "10.200.211.2")], 7)
    tcp = [p["Tcp"] for p in config["players"] if isinstance(p, dict) and "Tcp" in p]
    assert [(t["host"], t["port"], t["token"]) for t in tcp] == [("10.200.208.2", 31001, "tok0"),
                                                                  ("10.200.211.2", 31004, "tok3")]
    assert config["players"][1] == "QuickStart" and config["players"][2] == {"Empty": None}
    shown = redacted(config)
    assert [p["Tcp"]["token"] for p in shown["players"] if isinstance(p, dict) and "Tcp" in p] == ["***", "***"]
    assert tcp[0]["token"] == "tok0"                                          # the real config is untouched


def test_a_seat_network_is_internal_without_a_host_address_and_retries_an_overlap(tmp_path):
    gr, client = runner(tmp_path)
    net = MagicMock()
    client.networks.create.side_effect = [docker.errors.APIError("Pool overlaps with other one on this address space"),
                                          net]
    network, engine_ip = gr._seat_network("cw-52", 52, 1)
    assert network is net and engine_ip == "10.200.17.2"
    first, second = client.networks.create.call_args_list
    assert first.args == ("cw-52-s1",) and second.kwargs["internal"] is True
    assert second.kwargs["options"] == {"com.docker.network.bridge.inhibit_ipv4": "true"}
    assert second.kwargs["labels"] == {"codecraft.game": "52"}
    assert second.kwargs["ipam"]["Config"][0]["Subnet"] == "10.200.17.0/24"
    client.networks.create.side_effect = docker.errors.APIError("something else")
    with pytest.raises(docker.errors.APIError):
        gr._seat_network("cw-53", 53, 0)


def test_the_engine_joins_every_seat_network_at_its_fixed_address(tmp_path):
    gr, client = runner(tmp_path)
    seats = [tcp_seat(0, "10.200.208.2"), tcp_seat(1, "10.200.209.2")]
    for seat in seats:
        seat.network = MagicMock()
        seat.network.name = f"cw-52-s{seat.slot}"
    gr._start_engine("cw-52", seats, {"players": []}, ["a", "b"])
    kw = client.containers.create.call_args.kwargs
    assert kw["network"] == "cw-52-s0" and kw["pids_limit"] == 256
    assert kw["networking_config"] == {"cw-52-s0": {"IPAMConfig": {"IPv4Address": "10.200.208.2"}},
                                       "cw-52-s1": {"IPAMConfig": {"IPv4Address": "10.200.209.2"}}}
    assert kw["log_config"]["Config"] == {"max-size": "10m", "max-file": "1"}
    quick = Seat(slot=0, spec=PlayerSpec(Strategy(name="quickstart", builtin="QuickStart")))
    gr._start_engine("cw-53", [quick, quick], {"players": []}, ["q", "q"])
    kw = client.containers.create.call_args.kwargs
    assert kw["network_mode"] == "none" and "network" not in kw


def test_a_player_is_read_only_on_its_seat_network_with_a_bounded_log(tmp_path):
    gr, client = runner(tmp_path)
    seat = tcp_seat(2, "10.200.210.2")
    seat.network = MagicMock()
    seat.network.name = "cw-52-s2"
    gr._start_player("cw-52", seat, "cw-52-engine")
    kw = client.containers.create.call_args.kwargs
    assert kw["network"] == "cw-52-s2" and kw["command"] == ["cw-52-engine", "31003", "tok2"]
    assert kw["read_only"] is True and kw["tmpfs"] == {"/tmp": "rw,size=256m,mode=1777"}
    assert kw["log_config"]["Config"] == {"max-size": "10m", "max-file": "1"}
    assert {k: kw["environment"][k] for k in ("HOME", "TMPDIR", "DOTNET_CLI_HOME", "NUMBA_CACHE_DIR")} == \
        dict.fromkeys(("HOME", "TMPDIR", "DOTNET_CLI_HOME", "NUMBA_CACHE_DIR"), "/tmp")
    assert kw["cap_drop"] == ["ALL"] and kw["security_opt"] == ["no-new-privileges:true"]
    assert kw["mem_limit"] == kw["memswap_limit"] == "1024m" and kw["pids_limit"] == 256
    assert "runtime" not in kw                                               # the daemon's default


def test_docker_runtime_applies_to_players_but_not_to_a_gpu_seat(tmp_path):
    gr, client = runner(tmp_path)
    gr.cfg.data["docker"]["runtime"] = "runsc"
    seat = tcp_seat(0, "10.200.208.2")
    seat.network = MagicMock()
    gr._start_player("cw-52", seat, "cw-52-engine")
    assert client.containers.create.call_args.kwargs["runtime"] == "runsc"
    seat.spec.strategy.gpu = True
    gr._start_player("cw-52", seat, "cw-52-engine")
    kw = client.containers.create.call_args.kwargs
    assert "runtime" not in kw and kw["device_requests"]
