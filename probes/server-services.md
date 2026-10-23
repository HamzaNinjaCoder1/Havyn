# Server-PT services probe (S3.D0–D8)

## What was probed

A `Server-PT` device was created live and its JavaScript surface was read
through the Script Engine:

```js
var d = ipc.network().getDevice('s3probe-srv3');
Object.getOwnPropertyNames(Object.getPrototypeOf(d));
JSON.stringify(d.getModel());          // "Server-PT"
d.getPorts();                          // ["FastEthernet0"]
d.getRootModule().getModuleCount();    // 2
d.getProcess("DHCP");                  // throws
d.getProcess("DNS");                   // throws
```

Every `getProcess(<service name>)` call for `DHCP`, `DNS`, `HTTP`, `EMAIL`,
`FTP`, `AAA`, `NTP`, `SYSLOG`, `IoT` and `IoE` raised
`invalid string position`. The prototype contains only generic device methods
(`getCommandLine`, `getPorts`, `setDefaultGateway`, `setDhcpFlag`, `runCode…`,
module/serial helpers); there is no `setDnsRecord`, `addDhcpPool`,
`setHttpEnabled`, `addEmailUser`, `setFtpUser`, `setAaaServer` or equivalent.

`configureIosDevice` is an IOS helper. Server-PT has no IOS console, so it is
not a service configuration path either.

## Verdict

| Track | Feature | Status | Reason |
| --- | --- | --- | --- |
| S3.D0 | Server services API probe | **Refused** | No service-configuration API is exposed to the Script Engine. |
| S3.D1 | App-level DNS/HTTP proof | **Unverified** | No verified browser/DNS probe exists; the flow adapter only observes ICMP reachability. |
| S3.D2 | DNS service | **Refused** | No API to add records. |
| S3.D3 | HTTP/HTTPS service | **Refused** | No API to enable or set page content. |
| S3.D4 | DHCP server (Server-PT) | **Refused** | No API to add pools; use IOS DHCP on a router instead (already supported and live-proven). |
| S3.D5 | Email service | **Refused** | No API for users/domain. |
| S3.D6 | FTP service | **Refused** | No API for users/files. |
| S3.D7 | AAA (RADIUS/TACACS) | **Refused** | No API to configure the AAA server; router-side AAA can be configured but cannot be proven without the server. |
| S3.D8 | NTP/Syslog server | **Refused** | No API to enable the service; router-side NTP/Syslog config is supported. |

The limitation is the **MCP/Script-Engine API layer**, not the PT device model
or Havyn's compiler. A human can configure these services in the Server-PT GUI.

## Practical alternatives

* DHCP: use an IOS router pool (`dhcp_pools` / `subnet.dhcp`), including the
  relayed-LAN case (S3.A7).
* NTP/Syslog: configure the router side (`routing.management`); the server
  side must be configured manually in the PT GUI.
* DNS/HTTP/email/FTP: no Havyn path; a real server or an external emulator is
  required.

## Reproduction

The probe lives in the Stage 3 prototype dump documented above; the same
generic method list is reproducible with:

```python
from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
a = BridgeAdapter(host_bridge=False)
# create a Server-PT, then dump Object.getOwnPropertyNames(
# Object.getPrototypeOf(device)) and call getProcess(<name>).
```
