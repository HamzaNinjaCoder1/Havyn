# IoT registration probe (S3.G1–G2)

## What was probed

A `Smart LED` was created live and its JavaScript prototype was read:

```js
var d = ipc.network().getDevice('s3probe-iot');
JSON.stringify(Object.getOwnPropertyNames(Object.getPrototypeOf(d)));
```

The prototype exposes `digitalWrite`, `analogWrite`, `getComponentAtSlot`,
`getDigitalSlotsCount`, `getSerialOutputs`, CIP/OPC/Profinet/IEC-61850 toggles
and generic device methods. There is no method for a server address, account,
registration, or "register with Home Gateway".

The `S3.G1` probe therefore found **no registration API** to call. A
`HomeRouter-PT-AC` registration path was not proven either; without a device
side method, a gateway-side call alone cannot register the thing.

## Verdict

| Track | Feature | Status | Reason |
| --- | --- | --- | --- |
| S3.G1 | IoT registration probe | **Refused** | No device-side registration/server-address method in the Smart LED prototype. |
| S3.G2 | IoT registration + control | **Refused** | Registration is the prerequisite; `digitalWrite` alone is not a registered/controlled IoT device. |

The limitation is the **MCP/Script-Engine API layer**. A human can register the
device in the PT GUI.

## Practical alternative

Use the existing IoT/smart-home topology (T14) for cabling and reachability,
and treat registration/control as a manual GUI step. Do not mark it Verified
until a registration method is found and a live on/off effect is observed.

## Reproduction

Create a `Smart LED` and dump
`Object.getOwnPropertyNames(Object.getPrototypeOf(device))`; the output is the
evidence above. Search it for `regist`, `server`, `account`, `gateway`.
