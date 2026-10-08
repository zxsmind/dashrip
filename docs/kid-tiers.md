# KID tiers, key aliasing, and the 720p wall

This document explains the Widevine key-id system as observed on these
platforms, why "720p" is the practical ceiling for L3 devices, and the
key-aliasing step that makes cross-tier keys work.

## 1. The KID byte-1 tier system

A content key id (KID) is a 128-bit value. On the platforms studied here,
**the first two bytes encode the security tier** of the key:

```
KID (16 bytes, hex):
  0100 ...   SW_CRYPTO      (software, unrestricted)
  0101 ...   SW_DECRYPT     (software, restricted)
  0102 ...   HW_SECURE      (hardware-backed, HDCP required)
  0103 ...   HW_SECURE_ALL  (hardware-backed, strictest)
  0104 ...   HW_SECURE_ALL + (restricted variant)
  0105 ...   HW_SECURE_ALL + (restricted variant)
```

The *remaining 14 bytes* of the KID are the actual content identifier —
the tier bytes are a prefix layered on top of it.

Two consequences:

1. The **same content** can appear in a manifest with different KIDs
   depending on which tier the manifest authorizes for your session.
2. The keys the **license** gives you are tied to specific KIDs. If the
   MPD references a KID from a tier the license didn't issue keys for,
   decryption fails — even though the underlying 14-byte identifier is
   the same content.

## 2. Why L3 tops out at 720p (and why the MPD still shows 1080p)

Both platforms studied here follow the same model:

- The MPD advertises **all** representations (1080p, 720p, 540p, ...).
- The **license** decides which keys you actually get. An L3 device is
  offered keys for the SW tiers (`0100`/`0101`) at **720p or below**.
  The higher tiers (`0102`–`0105`) — which the 1080p+ representations
  reference — come back with **zero-length (all-zero) content keys**,
  i.e. "no key for you".

So if you see 1080p in the manifest but only get 720p keys, that is not a
bug: it is the tier system working as designed. An L1 device (TEE-backed)
is the only way to unlock the higher tiers.

This was verified empirically: with an L3 device, the 1080p
representations' KIDs (`0102`/`0103` variants) yielded zero CEKs from the
license, while the 720p representation's KID yielded a real 16-byte CEK.

> Note: some public reports claim these platforms cap L3 at 480p. In our
> measurements (TR region, EMEA edge) the L3 ceiling is **720p**. The
> ceiling can differ by region/account, so always verify what the license
> actually returns for your case.

## 3. Key aliasing (the fix that makes cross-tier work)

**The failure mode.** shaka-packager (and most CENC decryptors) match a
representation's `default_KID` against the keys you pass on the command
line, **byte for byte**. Consider a 720p video whose MPD representation
carries a HW-tier KID `0102 b5c3 ...` while the license — for your L3
session — issued the key under the SW-tier KID `0100 b5c3 ...`. Same
content, different prefix, no match:

```
shaka-packager: ERROR: Key for key_id=0102b5c3... was not found
```

**The fix.** Before building the decryptor's key list, register *every
real key under all six tier variants* of its KID:

```
for each (kid, key) with kid[0:1] == 0x01 and key != all-zero:
    body = kid[2:]                    # the 14-byte content id
    for tier in 0x00 .. 0x05:
        keys[ 0x01 + tier + body ] = key
```

Rules that matter:

- **Skip master/device keys** (the 128-hex / 64-byte widevine master
  secret) — aliasing them is wrong and useless.
- **Skip all-zero keys** — they are the "no key for this tier" markers,
  not content.
- The original KID is preserved (it is just one of the six variants).

After aliasing, any tier-variant KID in the manifest finds its key, and
the decryptor is agnostic to which tier the manifest used.

## 4. Quick reference

| KID prefix | Tier | Typical L3 outcome |
|---|---|---|
| `0100` | SW_CRYPTO | key issued (≤720p) |
| `0101` | SW_DECRYPT | key issued (≤720p) |
| `0102` | HW_SECURE | **zero CEK** for L3 |
| `0103` | HW_SECURE_ALL | **zero CEK** for L3 |
| `0104`/`0105` | restricted variants | **zero CEK** for L3 |

If your pipeline suddenly fails with "key not found" after a platform
change, this document is where to look first.
