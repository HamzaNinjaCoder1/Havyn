"""Stage 4 benchmark tests (S4.12) and adapter-parser tests."""

from __future__ import annotations

from pathlib import Path

from network_autopilot.adapter.base import PacketTracerAdapter
from network_autopilot.adapter.packet_tracer import js_commands as js
from network_autopilot.brownfield.benchmark import FaultInjectionBenchmark
from network_autopilot.brownfield.enhance_benchmark import run_enhancement_benchmark

LIBRARY = Path(__file__).resolve().parents[2] / "library"


def test_benchmark_executes_engine_and_scores_accuracy() -> None:
    result = FaultInjectionBenchmark(
        LIBRARY, include_repair=False, max_libraries=2
    ).run()
    summary = result.summary()
    assert summary["scenarios"] > 10
    assert summary["overall_accuracy"] >= 0.90
    assert summary["incorrect_fixes_left_in_place"] == 0
    assert result.confusion_matrix()
    assert result.to_markdown().startswith("# Stage 4 fault-injection benchmark")


def test_benchmark_tracks_multi_fault_cases() -> None:
    result = FaultInjectionBenchmark(
        LIBRARY, include_repair=False, max_libraries=3
    ).run()
    assert result.multi_fault_cases
    assert all("labels" in case for case in result.multi_fault_cases)


def test_enhancement_benchmark_enhances_library_networks() -> None:
    result = run_enhancement_benchmark(LIBRARY, target=5, max_networks=6)
    assert result.summary()["enhanced"] >= 5
    assert result.summary()["all_original_criteria_preserved"]


def test_ipconfig_parser_reads_gateway_and_dns() -> None:
    raw = """\
FastEthernet0 Connection:(default port)

   IPv4 Address....................: 10.99.0.10
   Subnet Mask.....................: 255.255.255.0
   Default Gateway.................: ::
                                     10.99.0.1
   DHCP Servers....................: 0.0.0.0
   DNS Servers.....................: ::
                                     10.99.0.53
"""
    parsed = js.parse_ipconfig_all(raw)
    assert parsed["ip"] == "10.99.0.10"
    assert parsed["mask"] == "255.255.255.0"
    assert parsed["gateway"] == "10.99.0.1"
    assert parsed["dns"] == ["10.99.0.53"]
    assert parsed["dhcp"] is False


def test_spanning_tree_parser_reads_blocking_state() -> None:
    raw = """\
VLAN0001
  Spanning tree enabled protocol ieee
  Interface        Role Sts Cost      Prio.Nbr Type
  ---------------- ---- --- --------- -------- ----
  Fa0/1            Root FWD 19        128.1    P2p
  Fa0/2            Altn BLK 19        128.2    P2p
"""
    rows = js.parse_show_spanning_tree(raw)
    assert rows == [
        {"vlan": 1, "interface": "FastEthernet0/1", "role": "Root", "state": "FWD"},
        {"vlan": 1, "interface": "FastEthernet0/2", "role": "Altn", "state": "BLK"},
    ]


def test_base_adapter_reports_unsupported_brownfield_reads() -> None:
    from network_autopilot.adapter.null_adapter import NullAdapter

    adapter = NullAdapter()
    assert adapter.read_spanning_tree("s1").status.value == "unsupported"
    assert adapter.read_running_config("r1").status.value == "unknown" or (
        adapter.read_running_config("r1").status.value == "unsupported"
    )


def test_show_ip_route_parser_reads_dynamic_codes() -> None:
    raw = """\
Codes: L - local, C - connected, S - static, O - OSPF
Gateway of last resort is 10.0.0.1 to network 0.0.0.0

O    10.0.2.0/24 [110/2] via 10.0.0.2, 00:01:02, GigabitEthernet0/1
C    10.0.1.0/24 is directly connected, GigabitEthernet0/0
S*   0.0.0.0/0 [1/0] via 10.0.0.1
"""
    routes = js.parse_show_ip_route(raw)
    kinds = {route["kind"] for route in routes}
    assert "ospf" in kinds
    assert "connected" in kinds
    assert "default" in kinds


def test_arp_parser_reads_host_and_router_rows() -> None:
    host = """  Internet Address      Physical Address      Type
  10.242.0.11           00e0.f96a.dad2        dynamic
"""
    router = """Protocol  Address          Age (min)  Hardware Addr   Type   Interface
Internet  10.242.0.10             2   00e0.f96a.dad2  ARPA   FastEthernet0/1
"""
    host_rows = js.parse_show_arp(host)
    router_rows = js.parse_show_arp(router)
    assert host_rows[0]["ip"] == "10.242.0.11"
    assert host_rows[0]["mac"] == "00e0.f96a.dad2"
    assert router_rows[0]["interface"] == "FastEthernet0/1"


def test_nat_parser_reads_translation_rows() -> None:
    raw = """Pro Inside global      Inside local       Outside local      Outside global
icmp 203.0.113.1:4      10.243.0.10:4      203.0.113.2:4      203.0.113.2:4
"""
    rows = js.parse_show_nat_translations(raw)
    assert rows[0]["inside_global"] == "203.0.113.1:4"
    assert rows[0]["inside_local"] == "10.243.0.10:4"
