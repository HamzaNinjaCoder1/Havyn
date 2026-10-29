# Stage 3 router probes (S3.A1–A16)

Live probes were run against Packet Tracer 9.x through the Script Engine file
channel on 2026-10-22/23. Each probe created one `s3p-router-*` device, pushed
the feature's CLI, read `show running-config` back, ran the named `show`
command, and deleted the device. Raw captures are under `probes/raw/`.

| Track | Feature | Live observation | Status |
| --- | --- | --- | --- |
| S3.A1 | OSPF single-area | `router ospf 1`, `router-id`, `network ... area 0` read back; `show ip route ospf` accepted. Live lab: 3 routers, 3 LANs, 3/3 flows PASS. | **Verified** |
| S3.A2 | OSPF multi-area | Config and full ABR adjacencies read back, but PT does not advertise area-0 prefixes into area 1; the cross-area flow stayed blocked in two topologies. | **Refused** (`probes/a2-multi-area-refusal.md`) |
| S3.A3 | EIGRP | `router eigrp 10` + `network` read back; live lab 2-router flow PASS. | **Verified** |
| S3.A4 | RIP v2 | `router rip`, `version 2`, `no auto-summary` read back; IOS classfully normalized the `network` line. Live lab 3-router flows PASS. | **Verified** |
| S3.A5 | default route + redistribution | `ip route 0.0.0.0 0.0.0.0 ...` and `default-information originate` read back; live lab PC→server flow PASS. | **Verified** |
| S3.A6 | NAT/PAT | `ip nat inside/outside`, ACL and `ip nat inside source list ... interface ... overload` read back; live T10 lab PC→public server flow PASS. | **Verified** |
| S3.A7 | DHCP relay | `ip helper-address` read back; live lab remote PC leased from the central IOS pool and reached the central LAN. | **Verified** |
| S3.A8 | HSRP | `standby 1 ip/priority/preempt` read back; `show standby brief` accepted; live failover lab PASS after the active router's LAN interface was shut down. | **Verified** |
| S3.A9 | Serial WAN end-to-end | DCE `clock rate` and `show controllers` read-back already proven by the serial adapter; live T04 serial lab flow PASS. | **Verified** |
| S3.A10 | GRE tunnel | The interface-source form works: `tunnel source GigabitEthernet0/0` + `tunnel mode gre ip`, and a live ping across the tunnel is 4/5. | **Verified** (`probes/raw/gre-tunnel.txt`) |
| S3.A11 | IPsec site-to-site | None of `crypto isakmp policy`, `crypto isakmp key`, `crypto ipsec transform-set`, `crypto map` or the ACL survived the push; PT accepted the block and stored none of it. | **Refused** |
| S3.A12 | Basic BGP | Per-device AS/neighbor/network config read back; live 2-AS lab flow PASS. | **Verified** (`probes/live/S3-A12-bgp.md`) |
| S3.A13 | IPv6 addressing | `ipv6 unicast-routing`, address and `ipv6 enable` read back; live IPv6 ping 5/5 between two routers. | **Verified** (`probes/raw/ipv6-ospfv3.txt`) |
| S3.A14 | OSPFv3 | With IPv6 enabled first, `ipv6 ospf 1 area 0` is accepted and both routers report FULL OSPFv3 neighbors. | **Verified** (`probes/raw/ipv6-ospfv3.txt`) |
| S3.A15 | SSH + local AAA | Config read back; the RSA key must be generated in **global config** (`crypto key generate rsa`, answer the modulus prompt) or `show ip ssh` says "SSH Disabled". The PC then reaches `Password:`, logs in, and the router shows `vty 0 admin` in `show users` and a `Session Started` entry in `show ssh`. | **Verified** (`probes/raw/S3.ssh-login.txt`) |
| S3.A16 | NTP, Syslog, SNMP | `ntp server`, `logging <host>` and `snmp-server community` read back; the server's Syslog process receives real router entries (host, timestamp, message). PT does not implement router NTP synchronisation (`show ntp status` is invalid and the clock stays at 1993), so time sync is unobservable. | **Verified** for logs (`probes/raw/S3.ntp-syslog.txt`) |

## Refusal evidence

* **S3.A11** — `probes/raw/S3.A11-router.txt`: the post-push `show
  running-config` contains no `crypto` line at all. The device model is 2911
  on PT 9.x; the limitation is the PT CLI/API layer, not Havyn. Practical
  alternative: use a GRE tunnel if the follow-up probe proves it, or terminate
  IPsec on a device/emulator outside PT.
* **S3.A2** — `probes/a2-multi-area-refusal.md`: the ABR is real (both
  adjacencies FULL) but PT does not advertise area-0 prefixes into area 1;
  two topologies reproduced it. Use single-area OSPF or static routes.
* **S3.A10** — the follow-up with an interface source
  (`probes/raw/gre-tunnel.txt`) works, so GRE is Verified, not refused.

## Reproduction

```bash
python probes/stage3_probe.py router
python probes/stage3_live_lab.py library/stage3/S3-A1-ospf-single.json
python probes/stage3_live_lab.py library/stage3/S3-A3-eigrp.json
python probes/stage3_live_lab.py library/stage3/S3-A2-ospf-multi-area.json --dump-config
python probes/stage3_live_lab.py library/stage3/S3-A8-hsrp.json --failover-device r1 --failover-interface all
python probes/stage3_live_lab.py library/stage3/S3-A12-bgp.json
python probes/stage3_ipv6_probe.py
python probes/stage3_gre_probe.py
python probes/stage3_ssh_lab.py
python probes/stage3_ntp_syslog_lab.py
```
