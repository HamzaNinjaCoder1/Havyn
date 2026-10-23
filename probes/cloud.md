# Cloud, modem and WAN probes (S3.F1–F4)

| Track | Feature | Status | Evidence / reason |
| --- | --- | --- | --- |
| S3.F1 | Cable modem | **Verified** | `library/T06-home-cable.json` has a live runner pass (`run-d0c3c88b4e28`, 60.1 s) and the home PC reaches the internet-side network. |
| S3.F2 | Explicit cloud mapping driver | **Unverified** | The compiler does not yet emit `addPortConnection`/`setDslConnection`; no live mapping probe has been run. The auto-mapped DSL topology T05 builds offline but has no live pass. |
| S3.F3 | ISP template | **Unverified** | No reusable ISP template spec/live pass yet. |
| S3.F4 | Cloud serial / Frame Relay | **Unverified** | The Cloud-PT serial port API was not probed; no live serial-through-cloud flow. |

## Note on the Cloud-PT API

The existing engine treats `cloud` and `modem` as unaddressable categories and
builds them through links/auto-mapping. A direct `addPortConnection` driver is
the missing piece for F2/F4; it must be probed live before it is claimed.
