# End devices and host DNS (S3.H1–H2)

| Track | Feature | Status | Evidence / reason |
| --- | --- | --- | --- |
| S3.H1 | Printer, IP phone, tablet, smartphone as flow endpoints | **Unverified** | The roles and catalogued models (`Printer-PT`, `7960`, `TabletPC-PT`, `SMARTPHONE-PT`) are now accepted by the schema and resolve to real PT models. No live address/flow test for these four devices yet. |
| S3.H2 | Host DNS setting | **Unverified** | The compiler now carries `host_dns` into the `configure_host` operation (and the bulk path already forwarded `dns`). The Script Engine exposes `configurePcIp(..., dns)`, but there is no verified read-back of the DNS field, so the value cannot yet be proven equal to the spec. |

## What is proven

* The schema accepts the four roles and maps each to its catalogued PT model
  (`tests/autopilot/test_stage3_features.py::test_new_end_device_roles_resolve_to_catalogued_models`).
* The host config op carries `dns` (`test_host_dns_reaches_the_configure_host_operation`).

## What would verify them

* H1: a live lab that addresses each device and runs an ICMP flow from it.
* H2: a live read-back method for the host's DNS field (currently absent), or
  an app-level DNS query that proves the resolver is used.
