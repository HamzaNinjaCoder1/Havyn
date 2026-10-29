# End devices and host DNS (S3.H1–H2)

## H1 — printer, tablet, smartphone, IP phone

| Device | Result | Evidence |
| --- | --- | --- |
| `Printer-PT` | Takes a static address; ICMP both ways. | `raw/S3.h1-end-devices.txt` |
| `TabletPC-PT` / `SMARTPHONE-PT` | Antenna association, DHCP .2/.3, flows both ways. | `raw/S3.e3-clients.txt` |
| `7960` IP phone | **Not an addressable endpoint**: no usable CLI, `Vlan1` accepts an address through the port API but `isPortUp()` stays false with the wall port linked and powered; no ping. | `raw/S3.h1-end-devices.txt` |

H1 is therefore **Refused** as a row: three of the four models work, but the
phone cannot be a flow source or destination. Alternative: use a PC/laptop
as the voice endpoint, or put a downstream PC behind the phone (the 7960 has
`Switch` and `PC` ports and can forward).

## H2 — host DNS

The PC's `DnsClient` reads back the configured server, and `ipconfig /all`
shows the same value:

```
DNS Servers.....................: ::
                                 10.88.0.2
```

`raw/S3.dns-http.txt` is the evidence; H2 is **Verified**.

## Reproduction

```bash
python probes/stage3_h1_lab.py
python probes/stage3_e3_lab.py
python probes/stage3_dns_http_lab.py
```
