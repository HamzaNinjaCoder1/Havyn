"""Stage 3 capability rows (S3.A1–S3.Z1).

Kept separate from the Stage 1/2 registry so the 53-track map is reviewable on
its own. Statuses follow the same rule as the rest of the map: Verified needs a
live PASS, Refused needs a live refusal with a reason, everything else is
honestly Unverified with a pointer to what is missing.
"""

from __future__ import annotations

LIVE = "probes/live/"
RAW = "probes/raw/"


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
        _row("S3.A15-ssh", "SSH and local AAA", ("router", "pc"),
             "Verified", RAW + "S3.ssh-login.txt "
             "(PC reached Password:, logged in; show users vty 0 admin and "
             "show ssh Session Started)"),
        _row("S3.A16-ntp-syslog-snmp", "NTP, Syslog, SNMP", ("router", "server"),
             "Verified", RAW + "S3.ntp-syslog.txt "
             "(router syslog entries arrived at the server; PT does not "
             "synchronise the router clock, so NTP time is unobservable)"),

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
             "Verified", RAW + "S3.asa-lab.txt "
             "(outside->DMZ reachable; outside->inside blocked)"),
        _row("S3.C3-asa-nat", "ASA object NAT/PAT", ("firewall:5506-X",),
             "Refused", RAW + "S3.asa-lab.txt",
             refusal_reason=(
                 "PT's ASA 9.6 image rejects every NAT form tested (object "
                 "NAT, twice NAT, classic nat/global) with CLI status 2, so "
                 "no translation can be configured. Use a router for NAT "
                 "(S3.A6) or terminate outside PT.")),
        _row("S3.C4-asa-routes", "ASA static/default routes",
             ("firewall:5506-X",), "Verified", RAW + "S3.asa-lab.txt "
             "(inside->remote network reachable through the static route)"),
        _row("S3.C5-asa-dhcp", "ASA DHCP server", ("firewall:5506-X",),
             "Verified", RAW + "S3.asa-lab.txt "
             "(inside PC leased 10.99.0.100 from the ASA)"),
        _row("S3.C6-object-group", "ASA object groups", ("firewall:5506-X",),
             "Refused", RAW + "S3.asa-lab.txt",
             refusal_reason=(
                 "PT's ASA 9.6 image rejects `object-group network` and any "
                 "ACL that references a group with CLI status 2. Use a plain "
                 "address-scoped ACL (S3.C1).")),

        # -- Track D: servers ------------------------------------------------
        _row("S3.D0-server-api", "Server-PT services API probe", ("server",),
             "Verified", RAW + "S3.processes.txt "
             "(DnsServer, HttpServer, DhcpServerMain, EmailServer, FtpServer, "
             "NtpServer, SyslogServer and AcsServer are exposed)"),
        _row("S3.D1-app-dns-http", "App-level DNS/HTTP proof", ("server", "pc"),
             "Verified", RAW + "S3.dns-http.txt "
             "(nslookup/ping server.local and an HTTP body returned)"),
        _row("S3.D2-dns", "DNS service", ("server", "pc"),
             "Verified", RAW + "S3.dns-http.txt "
             "(PC resolved server.local through 10.88.0.2)"),
        _row("S3.D3-http", "HTTP/HTTPS service", ("server", "pc"),
             "Verified", RAW + "S3.dns-http.txt "
             "(PC fetched the configured index.html body over HTTP; the "
             "HTTPS path was not separately exercised)"),
        _row("S3.D4-server-dhcp", "DHCP server (Server-PT)", ("server", "pc"),
             "Verified", RAW + "S3.server-dhcp.txt "
             "(POOL1 lease 10.77.0.100; DHCP server 10.77.0.2)"),
        _row("S3.D5-email", "Email service", ("server", "pc"),
             "Refused", RAW + "S3.server-misc.txt",
             refusal_reason=(
                 "The EmailServer configuration call (`updateAllAccounts`) "
                 "blocks the Script Engine and closes Packet Tracer, so no "
                 "mail flow can be driven. Use an external mail server.")),
        _row("S3.D6-ftp", "FTP service", ("server", "pc"),
             "Verified", RAW + "S3.ftp-login.txt "
             "(PC console `ftp 10.89.0.2` -> `230- Logged in`, `ftp>` prompt, "
             "`221- Service closing` on quit; the server's FTP process is read "
             "back enabled with the user account)"),
        _row("S3.D7-aaa", "AAA (RADIUS/TACACS) server", ("server", "router"),
             "Refused", RAW + "S3.aaa-login.txt",
             refusal_reason=(
                 "The ACS server accepts the user and client records and the "
                 "router accepts the radius-server and tacacs-server "
                 "configuration, but an SSH login against either path times "
                 "out; PT does not complete the AAA "
                 "exchange. Use local AAA (S3.A15) or an external AAA "
                 "server.")),
        _row("S3.D8-ntp-syslog-server", "NTP/Syslog server", ("server", "router"),
             "Verified", RAW + "S3.ntp-syslog.txt "
             "(NTP server configured and read back; five syslog entries from "
             "the router arrived)"),

        # -- Track E: wireless ----------------------------------------------
        _row("S3.E1-ap-association", "AccessPoint-PT association",
             ("access_point", "laptop"), "Verified",
             "tests/autopilot/test_live_connectivity.py"),
        _row("S3.E2-ssid-wpa2", "Custom SSID / WPA2", ("access_point", "laptop"),
             "Refused", RAW + "S3.e2-custom-ssid.txt",
             refusal_reason=(
                 "The AP accepts and reads back a custom SSID/WPA2, but every "
                 "documented client profile call (addProfile, "
                 "setCurrentProfile, setCurrentProfileStringIPs) is rejected "
                 "by this PT build and no antenna link forms. Use the default "
                 "SSID (S3.E1/S3.E3).")),
        _row("S3.E3-phone-tablet", "Smartphone/tablet wireless clients",
             ("smartphone", "tablet", "access_point"),
             "Verified", RAW + "S3.e3-clients.txt "
             "(both associated, DHCP .2/.3, flows both ways)"),
        _row("S3.E4-wlc", "WLC + lightweight APs", ("wlc", "access_point"),
             "Refused", RAW + "S3.wireless.txt",
             refusal_reason=(
                 "PT has no placeable lightweight AP model (LAP-PT/3702i are "
                 "rejected) and WLC-PT exposes only a base CapwapAC process, "
                 "so a WLC join cannot be built. Use autonomous APs.")),
        _row("S3.E5-multi-ssid", "Multiple SSIDs to VLANs",
             ("access_point",), "Refused", RAW + "S3.wireless-lab.txt",
             refusal_reason=(
                 "The AP's WirelessServer has one SSID and no SSID-to-VLAN "
                 "mapping method; PT offers no multi-SSID/VLAN API. Use "
                 "separate APs/VLANs or the default SSID.")),

        # -- Track F: modems/cloud/WAN --------------------------------------
        _row("S3.F1-cable-modem", "Cable modem", ("modem", "cloud", "pc"),
             "Verified", "library/T06-home-cable.json (live runner pass run-d0c3c88b4e28)"),
        _row("S3.F2-cloud-mapping", "Explicit cloud mapping driver",
             ("cloud", "modem", "router"), "Verified", RAW + "S3.cloud-lab.txt "
             "(two Frame Relay circuits on one cloud; customer A/B isolation "
             "blocked)"),
        _row("S3.F3-isp-template", "ISP template",
             ("cloud", "modem", "router", "server"), "Verified",
             RAW + "S3.cloud-lab.txt "
             "(home->internet and internet->home reachable)"),
        _row("S3.F4-cloud-serial", "Cloud serial / Frame Relay", ("cloud", "router"),
             "Verified", RAW + "S3.cloud-lab.txt "
             "(DLCI 100<->200 Frame Relay through the cloud; ping reachable)"),

        # -- Track G: IoT ----------------------------------------------------
        _row("S3.G1-iot-probe", "IoT registration API probe", ("iot",),
             "Verified", RAW + "S3.iot-api.txt "
             "(Smart-LED IoeClient exposes setupRemoteApi/reportStates; no "
             "IoT server process on the HomeRouter)"),
        _row("S3.G2-iot-control", "IoT registration + control", ("iot",),
             "Refused", RAW + "S3.iot-api.txt",
             refusal_reason=(
                 "The Smart-LED exposes IoeClient, but the HomeRouter has no "
                 "IoeServer and no registration/control path is exposed, so a "
                 "registered on/off control cannot be driven. Configure the "
                 "IoT service in the PT GUI.")),

        # -- Track H: end devices -------------------------------------------
        _row("S3.H1-end-devices", "Printer/IP phone/tablet/smartphone endpoints",
             ("printer", "ip_phone", "tablet", "smartphone"),
             "Refused", RAW + "S3.h1-end-devices.txt",
             refusal_reason=(
                 "Printer-PT, tablet and smartphone are proven endpoints, but "
                 "the 7960's Vlan1 never comes up (no usable CLI, "
                 "isPortUp false with the wall port linked and powered), so "
                 "the IP phone cannot be a flow endpoint. Use a PC/laptop as "
                 "the voice endpoint or a downstream PC behind the phone.")),
        _row("S3.H2-host-dns", "Host DNS setting", ("pc", "laptop", "server"),
             "Verified", RAW + "S3.dns-http.txt "
             "(DnsClient server 10.88.0.2 and ipconfig /all DNS 10.88.0.2)"),

        # -- Final ----------------------------------------------------------
        _row("S3.Z1-capability-map", "Capability Map publish + refusal review",
             ("docs",), "Verified", "docs/capability-map.md (live capability map publish)"),
    ]
    return rows
