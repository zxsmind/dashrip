# The Widevine device file (`.wvd`)

dashrip needs a Widevine **device file** to request licenses. This page
explains what it is, how dashrip uses it, and how to handle it safely.

## What it is

A `.wvd` file (binary, magic `WVD`) is a serialized Widevine device:

- a private key (the device's identity),
- its public key / certificate (system id, e.g. the familiar `8159`-style
  suffix),
- the **security level**: `L1` (hardware-backed, TEE) or `L3` (software).

It is, functionally, **a credential** — anyone holding it can request
DRM licenses as that device.

## Where it comes from

dashrip deliberately ships **no** device file, and you should obtain one
only for a device you own. In practice the community obtains `.wvd` files
by extracting them from a device (Android TV box, set-top, phone, ...)
you legitimately possess. There are also public collections of leaked
devices; using those is legally murky and operationally fragile (they get
revoked), so they are not recommended.

## Security level and what it unlocks

| Level | Typical ceiling on the studied platforms |
|---|---|
| L3 | up to **720p** (see [kid-tiers.md](kid-tiers.md) for the KID-tier mechanics) |
| L1 | up to the highest advertised tier (1080p+, region-dependent) |

The level you see in `doctor` output is read from the file itself.

## How dashrip uses it

1. `init` loads the file with `pywidevine.device.Device.load()` to verify
   it parses and to display level/system id.
2. At runtime, the device opens a session, receives the PSSH from the
   manifest, generates a license request, and parses the license
   response to extract the content keys (CEKs).
3. The file is opened read-only, never modified, and never transmitted.

## Keeping it safe

- Store it somewhere you keep credentials; it belongs in your
  `config/dashrip.json` **path only** — the config file (git-ignored)
  references it, it is never committed.
- Do not paste its contents anywhere. Its binary form is 2–4 KB; you do
  not need to see inside it.
- If you believe it has been exposed, treat it as compromised: the
  device may be reported or revoked by the licensor, and using a
  compromised device risks license denials.

## One device, many platforms

The same `.wvd` works for both supported platforms (HBO Max and Amazon
Prime) — the device is platform-agnostic; what differs is the license
server and its policies (see [platform-notes.md](platform-notes.md)).
