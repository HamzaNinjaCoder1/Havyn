# IoT registration probe (S3.G1–G2)

## What was probed

The first probe asked the Smart LED's device object for a registration method
and found none. The IpcAPI names the process that owns it:

```js
var led = ipc.network().getDevice("s3p-iot-led");
var c = led.getProcess("IoeClient");
// setupRemoteApi, reportStates
```

`probes/stage3_iot_probe.py` asks for `Ioe`, `IoeClient`, `IoeServer`,
`IoEComponent`, `CustomIO`, `WirelessRouter`, `AccessPointSwitcher` and
`WirelessServer` on a live Smart-LED and a HomeRouter-PT-AC. The result is in
`raw/S3.iot-api.txt`:

* the Smart LED has `IoeClient` with `setupRemoteApi` and `reportStates`;
* the HomeRouter has `WirelessServer` (SSID/WPA2) but **no** `IoeServer`,
  `Ioe` or registration process;
* the Server-PT's `IoeServer` exposes only base object methods, no
  registration/config surface;
* a direct Smart-LED ↔ HomeRouter copper link was refused by PT.

## Verdict

| Track | Feature | Status | Reason |
| --- | --- | --- | --- |
| S3.G1 | IoT registration API probe | **Verified** | The probe was completed: the client-side `IoeClient.setupRemoteApi` exists, but there is no server-side registration process on the gateway or Server-PT. |
| S3.G2 | IoT registration + control | **Refused** | Registration is the prerequisite and no server/registration path is exposed, so a registered on/off control cannot be driven. Configure the IoT service in the PT GUI. |

The limitation is the **MCP/Script-Engine API layer**, not the device model.

## Practical alternative

Use the existing IoT/smart-home topology (T14) for cabling and reachability,
and treat registration/control as a manual GUI step. Do not claim a registered
on/off effect until a server-side registration method is found and observed.

## Reproduction

```bash
python probes/stage3_iot_probe.py
```
