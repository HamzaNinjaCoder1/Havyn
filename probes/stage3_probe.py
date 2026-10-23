#!/usr/bin/env python
"""Stage 3 live capability probes.

Every probe creates exactly the devices it needs in a ``s3p-`` namespace,
pushes one feature's configuration, reads the running configuration and an
optional ``show`` command back from Packet Tracer's console, and deletes the
devices again.  Raw console captures are written under ``probes/raw/<group>.txt``
so another engineer can check the verdict against the exact PT output.

    python probes/stage3_probe.py             # run every probe
    python probes/stage3_probe.py router      # one group

The verdict for a feature is deliberately conservative:

    supported  the pushed lines are present in ``show running-config``
    refused    the push aborted and the lines are absent
    unknown    the console could not be read; never reported as supported
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw"
NAMESPACE = "s3p"


@dataclass
class Probe:
    group: str
    feature: str
    role: str
    model: str
    commands: list[str]
    base_commands: list[str] = field(
        default_factory=lambda: [
            "interface GigabitEthernet0/0",
            " ip address 10.99.0.1 255.255.255.0",
            " no shutdown",
            " exit",
            "interface GigabitEthernet0/1",
            " ip address 10.99.1.1 255.255.255.0",
            " no shutdown",
            " exit",
        ]
    )
    show: str | None = None
    markers: list[str] = field(default_factory=list)


ROUTER_PROBES = [
    Probe("router", "S3.A1 ospf-single", "router", "2911",
          ["router ospf 1", " router-id 1.1.1.1",
           " network 10.99.0.0 0.0.0.255 area 0", " exit"],
          show="show ip route ospf",
          markers=["router ospf 1", "network 10.99.0.0 0.0.0.255 area 0"]),
    Probe("router", "S3.A2 ospf-multi-area", "router", "2911",
          ["router ospf 2", " router-id 1.1.1.2",
           " network 10.99.0.0 0.0.0.255 area 0",
           " network 10.99.1.0 0.0.0.255 area 1", " exit"],
          show="show ip route ospf",
          markers=["network 10.99.0.0 0.0.0.255 area 0",
                   "network 10.99.1.0 0.0.0.255 area 1"]),
    Probe("router", "S3.A3 eigrp", "router", "2911",
          ["router eigrp 10", " network 10.99.0.0 0.0.0.255", " exit"],
          show="show ip route eigrp",
          markers=["router eigrp 10", "network 10.99.0.0"]),
    Probe("router", "S3.A4 ripv2", "router", "2911",
          ["router rip", " version 2", " network 10.99.0.0",
           " no auto-summary", " exit"],
          show="show ip route rip",
          markers=["router rip", "version 2", "network 10.99.0.0"]),
    Probe("router", "S3.A5 default-redistribute", "router", "2911",
          ["ip route 0.0.0.0 0.0.0.0 10.99.1.254",
           "router ospf 3", " default-information originate", " exit"],
          show="show ip route",
          markers=["ip route 0.0.0.0 0.0.0.0 10.99.1.254",
                   "default-information originate"]),
    Probe("router", "S3.A6 nat-pat", "router", "2911",
          ["interface GigabitEthernet0/0", " ip nat inside", " exit",
           "interface GigabitEthernet0/1", " ip nat outside", " exit",
           "access-list 1 permit 10.99.0.0 0.0.0.255",
           "ip nat inside source list 1 interface GigabitEthernet0/1 overload"],
          show="show ip nat translations",
          markers=["ip nat inside", "ip nat outside",
                   "ip nat inside source list 1 interface GigabitEthernet0/1 overload"]),
    Probe("router", "S3.A7 dhcp-relay", "router", "2911",
          ["interface GigabitEthernet0/0", " ip helper-address 10.99.1.10", " exit"],
          markers=["ip helper-address 10.99.1.10"]),
    Probe("router", "S3.A8 hsrp", "router", "2911",
          ["interface GigabitEthernet0/0", " standby 1 ip 10.99.0.254",
           " standby 1 priority 110", " standby 1 preempt", " exit"],
          show="show standby brief",
          markers=["standby 1 ip 10.99.0.254", "standby 1 priority 110"]),
    Probe("router", "S3.A10 gre-tunnel", "router", "2911",
          ["interface Tunnel0", " ip address 172.16.0.1 255.255.255.252",
           " tunnel source 10.99.0.1", " tunnel destination 10.99.1.1", " exit"],
          show="show ip interface brief",
          markers=["interface Tunnel0", "tunnel source 10.99.0.1",
                   "tunnel destination 10.99.1.1"]),
    Probe("router", "S3.A11 ipsec", "router", "2911",
          ["crypto isakmp policy 10", " encr aes", " hash sha256",
           " authentication pre-share", " exit",
           "crypto isakmp key LAB-REDACTED address 10.99.1.1",
           "crypto ipsec transform-set TS esp-aes esp-sha256-hmac",
           "access-list 100 permit ip 10.99.0.0 0.0.0.255 10.99.2.0 0.0.0.255",
           "crypto map CM 10 ipsec-isakmp", " set peer 10.99.1.1",
           " set transform-set TS", " match address 100", " exit",
           "interface GigabitEthernet0/1", " crypto map CM", " exit"],
          show="show crypto isakmp sa",
          markers=["crypto isakmp policy 10", "crypto map CM 10 ipsec-isakmp"]),
    Probe("router", "S3.A12 bgp", "router", "2911",
          ["router bgp 65001", " neighbor 10.99.1.2 remote-as 65002",
           " network 10.99.0.0 mask 255.255.255.0", " exit"],
          show="show ip bgp summary",
          markers=["router bgp 65001", "neighbor 10.99.1.2 remote-as 65002"]),
    Probe("router", "S3.A13 ipv6", "router", "2911",
          ["ipv6 unicast-routing",
           "interface GigabitEthernet0/0",
           " ipv6 address 2001:db8:99::1/64", " ipv6 enable", " exit"],
          show="show ipv6 route",
          markers=["ipv6 unicast-routing", "ipv6 address 2001:db8:99::1/64"]),
    Probe("router", "S3.A14 ospfv3", "router", "2911",
          ["ipv6 unicast-routing", "ipv6 router ospf 1",
           " router-id 1.1.1.3", " exit",
           "interface GigabitEthernet0/0", " ipv6 ospf 1 area 0", " exit"],
          show="show ipv6 route ospf",
          markers=["ipv6 router ospf 1", "ipv6 ospf 1 area 0"]),
    Probe("router", "S3.A15 ssh", "router", "2911",
          ["ip domain-name lab.local",
           "username admin privilege 15 secret LAB-REDACTED",
           "line vty 0 4", " login local", " transport input ssh", " exit",
           "ip ssh version 2"],
          markers=["ip domain-name lab.local", "username admin privilege 15",
                   "login local", "transport input ssh", "ip ssh version 2"]),
    Probe("router", "S3.A16 ntp-syslog-snmp", "router", "2911",
          ["ntp server 10.99.1.10", "logging host 10.99.1.11",
           "snmp-server community public RO"],
          markers=["ntp server 10.99.1.10", "logging 10.99.1.11",
                   "snmp-server community public RO"]),
]


SWITCH_PROBES = [
    Probe("switch", "S3.B1 trunk", "switch", "2960-24TT",
          ["interface FastEthernet0/1", " switchport mode trunk",
           " switchport trunk native vlan 99",
           " switchport trunk allowed vlan 10,20,99", " exit"],
          show="show interfaces trunk",
          markers=["switchport mode trunk", "switchport trunk allowed vlan 10,20,99"],
          base_commands=[]),
    Probe("switch", "S3.B2 svi-routing", "l3_switch", "3560-24PS",
          ["ip routing", "interface Vlan10",
           " ip address 10.99.10.1 255.255.255.0", " no shutdown", " exit"],
          markers=["ip routing", "ip address 10.99.10.1 255.255.255.0"],
          base_commands=[]),
    Probe("switch", "S3.B3 etherchannel", "switch", "2960-24TT",
          ["interface range FastEthernet0/1-2",
           " channel-group 1 mode active", " exit"],
          show="show etherchannel summary",
          markers=["channel-group 1 mode active"], base_commands=[]),
    Probe("switch", "S3.B4 stp", "switch", "2960-24TT",
          ["spanning-tree vlan 1 root primary",
           "interface FastEthernet0/1", " spanning-tree portfast",
           " spanning-tree bpduguard enable", " exit"],
          show="show spanning-tree",
          markers=["spanning-tree vlan 1 root primary", "spanning-tree portfast"],
          base_commands=[]),
    Probe("switch", "S3.B5 port-security", "switch", "2960-24TT",
          ["interface FastEthernet0/1", " switchport mode access",
           " switchport port-security", " switchport port-security maximum 1",
           " switchport port-security violation shutdown", " exit"],
          show="show port-security",
          markers=["switchport port-security", "switchport port-security maximum 1"],
          base_commands=[]),
    Probe("switch", "S3.B6 vtp", "switch", "2960-24TT",
          ["vtp mode server", "vtp domain S3LAB", "vtp password LAB-REDACTED"],
          show="show vtp status",
          markers=["vtp mode server", "vtp domain S3LAB"], base_commands=[]),
    Probe("switch", "S3.B7 voice-vlan", "switch", "2960-24TT",
          ["interface FastEthernet0/1", " switchport mode access",
           " switchport access vlan 10", " switchport voice vlan 20", " exit"],
          markers=["switchport voice vlan 20"], base_commands=[]),
    Probe("switch", "S3.B8 mgmt-ip", "switch", "2960-24TT",
          ["interface Vlan1", " ip address 10.99.8.2 255.255.255.0",
           " no shutdown", " exit", "ip default-gateway 10.99.8.1"],
          markers=["ip address 10.99.8.2 255.255.255.0",
                   "ip default-gateway 10.99.8.1"], base_commands=[]),
]


ASA_PROBES = [
    Probe("asa", "S3.C2 dmz-zone", "firewall", "5506-X",
          ["interface GigabitEthernet1/2", " nameif dmz", " security-level 50",
           " ip address 10.99.2.1 255.255.255.0", " no shutdown", " exit"],
          markers=["nameif dmz", "security-level 50"], base_commands=[]),
    Probe("asa", "S3.C3 object-nat", "firewall", "5506-X",
          ["object network INSIDE-NET", " subnet 10.99.0.0 255.255.255.0",
           " nat (inside,outside) dynamic interface", " exit"],
          show="show nat",
          markers=["object network INSIDE-NET", "nat (inside,outside) dynamic interface"],
          base_commands=[
              "interface GigabitEthernet1/1", " nameif inside",
              " security-level 100", " ip address 10.99.0.1 255.255.255.0",
              " no shutdown", " exit",
              "interface GigabitEthernet1/2", " nameif outside",
              " security-level 0", " ip address 10.99.1.1 255.255.255.0",
              " no shutdown", " exit"]),
    Probe("asa", "S3.C4 static-route", "firewall", "5506-X",
          ["route outside 0.0.0.0 0.0.0.0 10.99.1.254 1"],
          show="show route",
          markers=["route outside 0.0.0.0 0.0.0.0 10.99.1.254 1"],
          base_commands=[
              "interface GigabitEthernet1/2", " nameif outside",
              " security-level 0", " ip address 10.99.1.1 255.255.255.0",
              " no shutdown", " exit"]),
    Probe("asa", "S3.C5 dhcp-server", "firewall", "5506-X",
          ["dhcpd address 10.99.0.100-10.99.0.150 inside",
           "dhcpd enable inside"],
          show="show dhcpd",
          markers=["dhcpd address 10.99.0.100-10.99.0.150 inside",
                   "dhcpd enable inside"],
          base_commands=[
              "interface GigabitEthernet1/1", " nameif inside",
              " security-level 100", " ip address 10.99.0.1 255.255.255.0",
              " no shutdown", " exit"]),
    Probe("asa", "S3.C6 object-group", "firewall", "5506-X",
          ["object-group network WEB-SERVERS",
           " network-object host 10.99.2.10", " exit",
           "access-list OUTSIDE-IN extended permit tcp any object-group WEB-SERVERS eq 80"],
          show="show access-list",
          markers=["object-group network WEB-SERVERS",
                   "network-object host 10.99.2.10"],
          base_commands=[]),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_name(probe: Probe) -> str:
    tail = probe.feature.split()[0].replace(".", "").lower()
    return f"{NAMESPACE}-{probe.group}-{tail}"


def _read(adapter: BridgeAdapter, device: str, command: str) -> str:
    # configureIosDevice leaves the console at user EXEC (`Router>`), where
    # every `show running-config` is rejected. Enter enable first; a router
    # with no enable secret accepts it, and a failure is reported as UNKNOWN.
    adapter._console_show_full(device, "enable", timeout=12.0)
    result = adapter._console_show_full(device, command)
    if result.is_known:
        return str(result.value)
    return f"UNKNOWN: {result.detail or result.status.value}"


def run_probe(adapter: BridgeAdapter, probe: Probe) -> dict:
    name = _safe_name(probe)
    record = {
        "feature": probe.feature,
        "group": probe.group,
        "device": name,
        "model": probe.model,
        "commands": probe.commands,
        "probed_at": _now(),
    }
    try:
        adapter.delete_device(name)
    except Exception:
        pass
    receipt = adapter.create_device(
        name, probe.role, model_hint=probe.model, position=(120, 120)
    )
    record["create"] = receipt.result.value
    if receipt.result.value != "success":
        record["verdict"] = "blocked"
        record["detail"] = f"could not create probe device: {receipt.error}"
        return record
    time.sleep(3.0)
    try:
        if probe.base_commands:
            base = adapter.configure_ios(name, probe.base_commands)
            record["base_push"] = base.result.value
            time.sleep(1.5)
        push = adapter.configure_ios(name, probe.commands)
        record["push"] = push.result.value
        record["push_error"] = push.error
        time.sleep(2.0)
        config = _read(adapter, name, "show running-config")
        record["running_config"] = config
        missing = [m for m in probe.markers if m not in config]
        record["missing_markers"] = missing
        record["markers"] = probe.markers
        if probe.show:
            show = _read(adapter, name, probe.show)
            record["show_command"] = probe.show
            record["show_output"] = show
        if config.startswith("UNKNOWN") or "% Invalid input" in config:
            record["verdict"] = "unknown"
            record["detail"] = (
                "running-config could not be read (console rejected the read)"
            )
        elif missing:
            record["verdict"] = "refused"
            record["detail"] = "pushed lines absent from running-config"
        else:
            record["verdict"] = "supported"
            record["detail"] = "pushed lines present in running-config"
    except Exception as exc:  # a probe failure must not stop the matrix
        record["verdict"] = "unknown"
        record["detail"] = f"{type(exc).__name__}: {exc}"
        record["traceback"] = traceback.format_exc()
    finally:
        try:
            record["delete"] = adapter.delete_device(name).result.value
        except Exception as exc:
            record["delete"] = f"error: {exc}"
    return record


def main(argv: list[str]) -> int:
    groups = {
        "router": ROUTER_PROBES,
        "switch": SWITCH_PROBES,
        "asa": ASA_PROBES,
    }
    selected = argv[1:] or list(groups)
    unknown = [g for g in selected if g not in groups]
    if unknown:
        print(f"unknown group(s): {unknown}; choose from {sorted(groups)}")
        return 2
    adapter = BridgeAdapter(host_bridge=False)
    info = adapter.ping(timeout=5)
    if not info.reachable:
        print("PT UNREACHABLE:", info.detail)
        return 4
    RAW.mkdir(parents=True, exist_ok=True)
    results = []
    for group in selected:
        for probe in groups[group]:
            print(f"probe {probe.feature} ...", flush=True)
            record = run_probe(adapter, probe)
            results.append(record)
            raw_path = RAW / f"{probe.feature.split()[0]}-{probe.group}.txt"
            raw_path.write_text(
                "\n".join(
                    [
                        f"# {probe.feature} @ {record['probed_at']}",
                        f"# commands: {json.dumps(probe.commands)}",
                        f"# verdict: {record['verdict']} -- {record.get('detail', '')}",
                        "",
                        "## show running-config",
                        record.get("running_config", ""),
                        "",
                        f"## {record.get('show_command', 'show')}",
                        record.get("show_output", ""),
                    ]
                ),
                encoding="utf-8",
            )
            print(f"  -> {record['verdict']} ({record.get('detail', '')})", flush=True)
    out = ROOT / "stage3-probe-results.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nwrote {out} and {RAW}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
