# Expansion Modules

The catalog includes **151 expansion modules** (WICs, HWICs, NIMs, NMs, SFPs,
wireless/cellular adapters, ...). Read `pt/catalog/modules.py` to discover exact names -
optionally filtered by router model or category - then a module operation (one) or
a batch (many).

## Critical rules

!!! danger "`slot` is a STRING, not an integer"
    PT compares the slot with `===` against its internal map. Passing `0` (int) does
    **not** match `"0/0"` and `addModule()` silently returns `false`. Always use a
    string literal.

| Slot type | Format | Example |
|-----------|--------|---------|
| HWIC on 1941 / 2901 / 2911 | `"0/0"`, `"0/1"`, `"0/2"`, `"0/3"` | module op `{device:"R1", slot:"0/0", module:"HWIC-2T"}` |
| NIM on ISR4321 / ISR4331 | `"0/1"`, `"0/2"` (chassis/subslot — **not** `"0"`/`"1"`) | module op `{device:"R1", slot:"0/1", module:"NIM-2T"}` |
| NM on 2811 / 2620XM / Router-PT | `"1"` | module op `{device:"R1", slot:"1", module:"NM-4A/S"}` |
| Cloud / hosts | `"0"`, `"1"`, … `"7"` | module op `{device:"Cloud", slot:"0", module:"PT-CLOUD-NM-1S"}` |

## Compatibility

- **2911 / 2901 / 1941 (ISR G2)** → **HWIC/WIC only** (no NM). For 4 serial ports,
  install 2× `HWIC-2T` in slots `"0/0"` and `"0/1"`.
- **ISR4321 / ISR4331** → **NIM only** (`NIM-2T` for serial, `NIM-ES2-4` for GigE).
- **Router-PT** → `PT-ROUTER-NM-*` in slots `"0".."6"`.

## Port naming

Ports are named `<type><chassis>/<subslot>/<port>`:

- `HWIC-2T` in slot `"0/0"` → `Serial0/0/0`, `Serial0/0/1`
- `HWIC-2T` in slot `"0/1"` → `Serial0/1/0`, `Serial0/1/1`

## Installing several at once

!!! tip "Prefer a batch install"
    It powers off → adds all modules → powers on in **one** `runCode`. Multiple
    individual installs each power-cycle the device, which is slower and
    can make a single call report a (false) timeout while the reboot finishes.

```text
install_modules([
  {"device": "R1", "slot": "0/0", "module": "HWIC-2T"},
  {"device": "R1", "slot": "0/1", "module": "HWIC-2T"}
])
# → Serial0/0/0, 0/0/1, 0/1/0, 0/1/1
```
