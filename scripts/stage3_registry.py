"""Stage 3 capability rows (S3.A1–S3.Z1).

Kept separate from the Stage 1/2 registry so the 53-track map is reviewable on
its own. Statuses follow the same rule as the rest of the map: Verified needs a
live PASS, Refused needs a live refusal with a reason, everything else is
honestly Unverified with a pointer to what is missing.
"""

from __future__ import annotations

LIVE = "probes/live/"


def _row(
    id: str,
    name: str,
    families: tuple[str, ...],
    status: str,
    evidence: str,
    refusal_reason: str = "",
    topology: str = "",
) -> dict:
    return {
        "id": id,
        "name": name,
        "families": families,
        "status": status,
        "evidence": evidence,
        "refusal_reason": refusal_reason,
        "topology": topology,
    }


def stage3_features() -> list[dict]:
    rows = [
        # -- Track A: routers ------------------------------------------------
        _row("S3.A1-ospf-single", "OSPF single-area routing", ("router",),
             "Verified", LIVE + "S3-A1-ospf-single.md"),
        _row("S3.A2-ospf-multi-area", "OSPF multi-area (ABR, inter-area routes)",
             ("router",), "Refused", "probes/a2-multi-area-refusal.md",
             refusal_reason=(
                 "PT accepts the ABR config and both neighbors reach FULL, "
                 "but it does not advertise area-0 prefixes into area 1; "
                 "the cross-area flow stays blocked. Use single-area OSPF "
                 "(S3.A1) or static inter-area routes.")),
        _row("S3.A3-eigrp", "EIGRP routing", ("router",),
             "Verified", LIVE + "S3-A3-eigrp.md"),
        _row("S3.A4-ripv2", "RIP v2 routing", ("router",),
             "Verified", LIVE + "S3-A4-ripv2.md"),
        _row("S3.A5-default-redistribute",
             "Default route + default-information originate", ("router",),
             "Verified", LIVE + "S3-A5-default-redistribute.md "
             "(flow PASS; static-route read-back still reports missing)"),
        _row("S3.A6-nat-pat", "NAT/PAT overload", ("router",),
             "Verified", LIVE + "T10-nat-internet.md "
             "(flow PASS; default-route read-back still reports missing)"),
        _row("S3.A7-dhcp-relay", "DHCP relay to a central server", ("router",),
             "Verified", LIVE + "S3-A7-dhcp-relay.md "
             "(remote DHCP lease + flow PASS; static-route read-back still "
             "reports missing)"),
        _row("S3.A8-hsrp", "HSRP gateway failover", ("router",),
             "Verified", LIVE + "S3-A8-hsrp.md"),
        _row("S3.A9-serial-wan", "Serial WAN end-to-end (DCE clock)",
             ("router",), "Verified", LIVE + "T04-serial-wan.md "
             "(flow PASS; static-route read-back still reports missing)"),
        _row("S3.A10-gre-tunnel", "GRE tunnel", ("router",),
             "Verified",
             "probes/raw/gre-tunnel.txt (interface source + "
             "'tunnel mode gre ip'; live ping 4/5)"),
        _row("S3.A11-ipsec", "IPsec site-to-site", ("router",),
             "Refused", "probes/raw/S3.A11-router.txt",
             refusal_reason=(
                 "PT accepted the push but stored none of the crypto isakmp / "
                 "ipsec transform-set / crypto map lines; no live tunnel is "
                 "possible on this model. Use GRE if its follow-up probe "
                 "succeeds, or terminate IPsec outside PT.")),
        _row("S3.A12-bgp", "Basic BGP", ("router",),
             "Verified", LIVE + "S3-A12-bgp.md"),
        _row("S3.A13-ipv6", "IPv6 addressing and static routes", ("router",),
             "Verified", "probes/raw/ipv6-ospfv3.txt "
             "(live IPv6 ping 5/5 and connected route read-back)"),
        _row("S3.A14-ospfv3", "OSPFv3", ("router",),
             "Verified", "probes/raw/ipv6-ospfv3.txt "
             "(live OSPFv3 neighbors FULL in both directions)"),
        _row("S3.A15-ssh", "SSH and local AAA", ("router",),
             "Unverified",
             "probes/raw/S3.A15-router.txt (server config read back; PC SSH "
             "login not observable)"),
        _row("S3.A16-ntp-syslog-snmp", "NTP, Syslog, SNMP", ("router",),
             "Unverified",
             "probes/raw/S3.A16-router.txt (router config read back; server "
             "effect unobservable)"),

        # -- Track B: switches ----------------------------------------------
        _row("S3.B1-trunk", "Trunk read-back", ("switch",),
             "Verified", "probes/live/switch-probes.md"),
        _row("S3.B2-svi-routing", "L3 switch SVI + ip routing",
             ("l3_switch",), "Verified", "probes/live/switch-probes.md"),
        _row("S3.B3-etherchannel", "EtherChannel (LACP)",
             ("switch", "l3_switch"), "Verified", "probes/live/switch-probes.md"),
        _row("S3.B4-stp", "STP root/PortFast/BPDU guard", ("switch",),
             "Verified", "probes/live/switch-probes.md"),
        _row("S3.B5-port-security", "Port-security", ("switch",),
             "Verified", "probes/live/switch-probes.md"),
        _row("S3.B6-vtp", "VTP domain/mode", ("switch",),
             "Verified", "probes/live/switch-probes.md"),
        _row("S3.B7-voice-vlan", "Voice VLAN", ("switch",),
             "Verified", "probes/live/switch-probes.md"),
        _row("S3.B8-mgmt-ip", "Switch management IP", ("switch",),
             "Verified", "probes/live/switch-probes.md"),

        # -- Track C: ASA ----------------------------------------------------
        _row("S3.C1-address-scoped-acl", "Address-scoped ASA ACL",
             ("firewall:5506-X", "firewall:5505"), "Verified",
             "tests/autopilot/test_live_policy.py::"
             "test_asa_address_scoped_acl_is_enforced"),
        _row("S3.C2-dmz-zone", "ASA DMZ third zone", ("firewall:5506-X",),
             "Unverified", "probes/asa.md"),
        _row("S3.C3-asa-nat", "ASA object NAT/PAT", ("firewall:5506-X",),
             "Unverified", "probes/asa.md"),
        _row("S3.C4-asa-routes", "ASA static/default routes",
             ("firewall:5506-X",), "Unverified", "probes/asa.md"),
        _row("S3.C5-asa-dhcp", "ASA DHCP server", ("firewall:5506-X",),
             "Unverified", "probes/asa.md"),
        _row("S3.C6-object-group", "ASA object groups", ("firewall:5506-X",),
             "Unverified", "probes/asa.md"),

        # -- Track D: servers ------------------------------------------------
        _row("S3.D0-server-api", "Server-PT services API probe", ("server",),
             "Refused", "probes/server-services.md",
             refusal_reason=(
                 "No service-configuration method is exposed: getProcess() "
                 "rejects every service name and the prototype has no "
                 "DNS/DHCP/HTTP/email/FTP method. The limitation is the "
                 "MCP/Script-Engine API layer; configure services in the PT "
                 "GUI.")),
        _row("S3.D1-app-dns-http", "App-level DNS/HTTP proof", ("server", "pc"),
             "Unverified", "probes/server-services.md"),
        _row("S3.D2-dns", "DNS service", ("server",), "Refused",
             "probes/server-services.md",
             refusal_reason="No Script-Engine API to add DNS records."),
        _row("S3.D3-http", "HTTP/HTTPS service", ("server",), "Refused",
             "probes/server-services.md",
             refusal_reason="No Script-Engine API to enable HTTP or set content."),
        _row("S3.D4-server-dhcp", "DHCP server (Server-PT)", ("server",),
             "Refused", "probes/server-services.md",
             refusal_reason=(
                 "No Script-Engine API to add pools. Use an IOS router pool "
                 "instead (supported, incl. S3.A7 relay).")),
        _row("S3.D5-email", "Email service", ("server",), "Refused",
             "probes/server-services.md",
             refusal_reason="No Script-Engine API for email users/domain."),
        _row("S3.D6-ftp", "FTP service", ("server",), "Refused",
             "probes/server-services.md",
             refusal_reason="No Script-Engine API for FTP users/files."),
        _row("S3.D7-aaa", "AAA (RADIUS/TACACS) server", ("server", "router"),
             "Refused", "probes/server-services.md",
             refusal_reason=(
                 "No API to configure the AAA server; router-side AAA can be "
                 "rendered but cannot be proven without the server.")),
        _row("S3.D8-ntp-syslog-server", "NTP/Syslog server", ("server",),
             "Refused", "probes/server-services.md",
             refusal_reason=(
                 "No API to enable the service; router-side NTP/Syslog is "
                 "supported but the server effect is unobservable.")),

        # -- Track E: wireless ----------------------------------------------
        _row("S3.E1-ap-association", "AccessPoint-PT association",
             ("access_point", "laptop"), "Verified",
             "tests/autopilot/test_live_connectivity.py"),
        _row("S3.E2-ssid-wpa2", "Custom SSID / WPA2",
             ("access_point",), "Unverified", "probes/wireless.md"),
        _row("S3.E3-phone-tablet", "Smartphone/tablet wireless clients",
             ("smartphone", "tablet"), "Unverified", "probes/wireless.md"),
        _row("S3.E4-wlc", "WLC + lightweight APs", ("wlc", "access_point"),
             "Unverified", "probes/wireless.md"),
        _row("S3.E5-multi-ssid", "Multiple SSIDs to VLANs",
             ("access_point",), "Unverified", "probes/wireless.md"),

        # -- Track F: modems/cloud/WAN --------------------------------------
        _row("S3.F1-cable-modem", "Cable modem", ("modem", "cloud", "pc"),
             "Verified", "library/T06-home-cable.json (live runner pass run-d0c3c88b4e28)"),
        _row("S3.F2-cloud-mapping", "Explicit cloud mapping driver",
             ("cloud", "modem"), "Unverified", "probes/cloud.md"),
        _row("S3.F3-isp-template", "ISP template", ("cloud", "modem", "router"),
             "Unverified", "probes/cloud.md"),
        _row("S3.F4-cloud-serial", "Cloud serial / Frame Relay", ("cloud",),
             "Unverified", "probes/cloud.md"),

        # -- Track G: IoT ----------------------------------------------------
        _row("S3.G1-iot-probe", "IoT registration API probe", ("iot",),
             "Refused", "probes/iot.md",
             refusal_reason=(
                 "The Smart LED prototype has no server-address/registration "
                 "method; digitalWrite is not registration. Configure in the "
                 "PT GUI.")),
        _row("S3.G2-iot-control", "IoT registration + control", ("iot",),
             "Refused", "probes/iot.md",
             refusal_reason=(
                 "Registration is the prerequisite and has no API, so a "
                 "registered on/off control cannot be proven.")),

        # -- Track H: end devices -------------------------------------------
        _row("S3.H1-end-devices", "Printer/IP phone/tablet/smartphone endpoints",
             ("printer", "ip_phone", "tablet", "smartphone"),
             "Unverified", "probes/end-devices.md"),
        _row("S3.H2-host-dns", "Host DNS setting", ("pc", "laptop", "server"),
             "Unverified", "probes/end-devices.md"),

        # -- Final ----------------------------------------------------------
        _row("S3.Z1-capability-map", "Capability Map publish + refusal review",
             ("docs",), "Verified", "docs/capability-map.md (live capability map publish)"),
    ]
    return rows
