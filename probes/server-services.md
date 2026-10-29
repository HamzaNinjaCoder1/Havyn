# Server-PT services (S3.D0–D8)

## What changed

The first probe asked `getProcess("DNS")`, `"DHCP"`, `"HTTP"`, `"FTP"` and
concluded there was no service API. The PT IpcAPI documents the real names and
`probes/stage3_process_probe.py` asks for them. They exist:

| Process | Methods used by the probe/lab |
| --- | --- |
| `DnsServer` | `setEnable`, `addIpAddress`, `addARecordToNameServerDb`, `getIpAddOfDomain`, `getEntryCount` |
| `HttpServer` / `HttpsServer` | `setEnable`, `setPageContents`, `getPage`, `setPortNumber` |
| `DhcpServerMain` | `getDhcpServerProcessByPortName` → `addNewPool`, `removePool`, `setEnable`, `getPool` |
| `EmailServer` | `addUser`, `updateAllAccounts`, `getAllEmailAcctAsStrings` |
| `SmtpServer` / `Pop3Server` | `setEnable`, `setServerDomainName` |
| `FtpServer` | `setEnabled`, `getFtpUserAccountManager().addFtpUser` |
| `NtpServer` | `setEnabled`, `setRefClockTime`, `getRefClockTime` |
| `SyslogServer` | `setEnable`, `getAllEntries`, `clearAllSysLogEntries` |
| `AcsServer` | `addToUserMap`, `addToClientMap`, `enableACSServerService` |

The PC side is equally real: `DnsClient`, `HttpClient`, `EmailClient`,
`FtpClient`, `SmtpClient`, `Pop3Client`, `NtpClient`.

## Verdict

| Track | Feature | Status | Evidence |
| --- | --- | --- | --- |
| S3.D0 | Server services API probe | **Verified** | `raw/S3.processes.txt` — every service object exists with a configuration surface. |
| S3.D1 | App-level DNS/HTTP proof | **Verified** | `raw/S3.dns-http.txt` — `nslookup`/`ping server.local` resolve; `HttpClient` returns the configured page. |
| S3.D2 | DNS service | **Verified** | same file — PC resolved `server.local` through 10.88.0.2. |
| S3.D3 | HTTP service | **Verified** | same file — body `HAVYN-APP-PROOF` came back from `index.html` over HTTP; the HTTPS path was not separately exercised. |
| S3.D4 | DHCP server | **Verified** | `raw/S3.server-dhcp.txt` — POOL1 lease 10.77.0.100, DHCP server 10.77.0.2. |
| S3.D5 | Email service | **Refused** | `raw/S3.server-misc.txt` — the `EmailServer`/`updateAllAccounts` call blocks the Script Engine and closes PT; no mail flow can be driven. |
| S3.D6 | FTP service | **Verified** | `raw/S3.ftp-login.txt` — PC console `ftp` logs in (`230- Logged in`) and quits (`221- Service closing`); the server process is enabled with the user account. |
| S3.D7 | AAA server | **Refused** | `raw/S3.aaa-login.txt` — ACS users/clients and router aaa/radius/tacacs config are accepted, but an SSH login against both RADIUS and TACACS+ times out. |
| S3.D8 | NTP/Syslog server | **Verified** | `raw/S3.ntp-syslog.txt` — NTP server configured; five router syslog entries arrived. |

## Reproduction

```bash
python probes/stage3_process_probe.py
python probes/stage3_dns_http_lab.py
python probes/stage3_server_dhcp_lab.py
python probes/stage3_ntp_syslog_lab.py
python probes/stage3_server_ftp_aaa.py
```

Do **not** run the email `updateAllAccounts`/`sendMail` path on the Script
Engine file channel: it stalls PT. The server-side email API is documented in
`S3.server-misc.txt`, not claimed as working.
