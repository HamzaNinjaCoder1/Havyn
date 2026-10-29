# Stage 3 probe and live-lab evidence

Everything here is reproducible against a running Packet Tracer on the Script
Engine file channel. Nothing in this directory is hand-written evidence: the
`raw/` files are verbatim console captures, and `live/` is written by
`stage3_live_lab.py` after the flows run.

| Path | What it is |
| --- | --- |
| `stage3_probe.py` | Router, switch and ASA feature probes; writes `raw/` and `stage3-probe-results.json`. |
| `stage3_live_lab.py` | One live lab: creates the compiler's DAG, runs the flow criteria, writes `live/<spec>.{md,json}`, deletes only the names it created. |
| `stage3_ipv6_probe.py` | Two-router IPv6 + OSPFv3 probe, including a real IPv6 ping. |
| `stage3_gre_probe.py` | Two-router GRE tunnel probe with `tunnel source <interface>` and a real ping. |
| `stage3_asa_read.py`, `stage3_asa_shows.py` | ASA boot wait, push and console/show probes. |
| `stage3_asa_readback.py`, `stage3_asa_lab.py` | ASA `enterCommand(..., "enable")` read-back; DMZ/routes/DHCP flows and C3/C6 statuses. |
| `stage3_process_probe.py` | Real `getProcess` names and their method surfaces (server services, wireless, router). |
| `stage3_dns_http_lab.py` | App-level DNS (`nslookup`/`ping server.local`) and HTTP body proof. |
| `stage3_server_dhcp_lab.py` | Server-PT DHCP pool + client lease. |
| `stage3_ntp_syslog_lab.py` | Router NTP/Syslog against the server; real syslog entries. |
| `stage3_server_ftp_aaa.py` | FTP/AAA server config surface (no blocking email call). |
| `stage3_ftp_lab.py` | PC console FTP login to the Server-PT FTP service. |
| `stage3_aaa_lab.py` | AAA-backed SSH login attempt; records the live timeout. |
| `stage3_ssh_lab.py` | RSA key generation, PC SSH password login, `show users`/`show ssh`. |
| `stage3_e2_probe.py`, `stage3_e3_lab.py`, `stage3_wireless_lab.py`, `stage3_wireless_probe.py` | Custom-SSID refusal, smartphone/tablet association + DHCP + flows, WLC/LAP availability. |
| `stage3_cloud_probe.py`, `stage3_cloud_lab.py` | Cloud mapping signatures; F2/F4 Frame Relay and F3 ISP template. |
| `stage3_h1_lab.py` | Printer endpoint proof and the 7960's down-Vlan1 refusal. |
| `stage3_iot_probe.py` | IoT process surface on Smart-LED/HomeRouter. |
| `raw/` | Verbatim PT output for every probe. |
| `live/` | Live-lab records and the switch probe report. |
| `failing-first.txt` | The new tests failing on the Stage 2 baseline and passing on this tree. |
| `a2-multi-area-refusal.md` | The OSPF multi-area refusal evidence. |
| `router.md`, `asa.md`, `wireless.md`, `cloud.md`, `end-devices.md`, `server-services.md`, `iot.md` | Per-track probe summaries and verdicts. |

## Recommended order

```bash
python probes/stage3_probe.py
python probes/stage3_live_lab.py library/stage3/S3-A1-ospf-single.json
python probes/stage3_live_lab.py library/stage3/S3-A2-ospf-multi-area.json --dump-config
python probes/stage3_live_lab.py library/stage3/S3-A3-eigrp.json
python probes/stage3_live_lab.py library/stage3/S3-A4-ripv2.json
python probes/stage3_live_lab.py library/stage3/S3-A5-default-redistribute.json
python probes/stage3_live_lab.py library/stage3/S3-A7-dhcp-relay.json
python probes/stage3_live_lab.py library/stage3/S3-A8-hsrp.json --failover-device r1 --failover-interface all
python probes/stage3_live_lab.py library/stage3/S3-A12-bgp.json
python probes/stage3_live_lab.py library/T04-serial-wan.json
python probes/stage3_live_lab.py library/T10-nat-internet.json
python probes/stage3_live_lab.py library/stage3/S3-A15-ssh.json
python probes/stage3_live_lab.py library/stage3/S3-D1-dns-http.json
python probes/stage3_live_lab.py library/stage3/S3-D4-server-dhcp.json
python probes/stage3_live_lab.py library/stage3/S3-D8-ntp-syslog.json
python probes/stage3_live_lab.py library/stage3/S3-E3-wireless-clients.json
python probes/stage3_live_lab.py library/stage3/S3-F2-cloud-mapping.json
python probes/stage3_live_lab.py library/stage3/S3-F3-isp-template.json
python probes/stage3_live_lab.py library/stage3/S3-F4-cloud-frame-relay.json
python probes/stage3_ipv6_probe.py
python probes/stage3_gre_probe.py
python probes/stage3_ssh_lab.py
python probes/stage3_ntp_syslog_lab.py
python probes/stage3_ftp_lab.py
python probes/stage3_aaa_lab.py
```

The labs delete their own devices in a `finally` block. `--keep` is for
debugging and prints the names it left behind so they can be removed.

## Honest limits

* The ASA read-back uses `Asa::enterCommand(cmd, "enable")`. C3 NAT and C6
  object groups are genuinely rejected by PT's ASA image (CLI status 2).
* Server-PT services are reachable through documented process names. The
  email `updateAllAccounts`/`sendMail` path blocks the Script Engine and
  closes PT; FTP logs in from the PC console (`230- Logged in`); an AAA
  login against both RADIUS and TACACS+ times out (refused).
* Custom SSID/WPA2 works on the AP but the client profile API is rejected by
  this PT build; default-SSID associations are the proven path.
* The 7960's `Vlan1` never comes up, so the IP phone is not an endpoint.
* The A5/A6/A7/A9 labs show a PASSing flow while the adapter still reports a
  static route missing on read-back. The flow is the stronger proof; the
  read-back defect is recorded, not hidden.
