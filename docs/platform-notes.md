# Platform notes

Observations about the two supported platforms, with the
evidence behind each. These notes explain *why* the pipeline is shaped the
way it is, and what to expect when a platform changes.

## HBO Max

### Auth model (stable)

- One long-lived session cookie (`st=` JWT, observed expiries years out)
  is the entire authentication story for CMS and playback.
- The `playbackInfo` POST needs, besides the cookie, a set of
  client-identity headers captured from the browser
  (`x-wbd-ace`, `x-wbd-session-state`, `x-fingerprint-id`,
  `x-disco-client`, `x-disco-params`, `x-device-info`, ...). A generic
  header set gets HTTP 400; the captured set gets 200.
- The request body is a rich JSON template (device description, player
  version, IAB string, ...). Only the **editId** changes per item — the
  rest of the template works as captured.

### Media delivery

- Content tracks are **single complete files** (SegmentBase, one BaseURL
  per representation). No segment loop, no playlist refresh.
- The signed manifest URL and the CDN media URLs **do not need the
  cookie** — the token lives in the URL. Downloads are plain HTTPS with
  UA/Origin/Referer, support `Range`, and resume cleanly.
- The CDN edge used is an Akamai EU node (`akm.eu.prd.media.max.com`);
  requests from other data-center locations in the EU have been observed
  to work (useful for server-side downloading).

### The dynamic service certificate

The Widevine service certificate rotates and is embedded in **every**
`playbackInfo` response. Always take it from the live response; a cached
copy will eventually break license requests.

## Amazon Prime Video

### The 2026 architecture (from HAR analysis)

```
StartSession        POST  -> sessionToken
GetVodPlaybackResources  POST -> MPD + service certificate + timedTextUrls
GetWidevineLicense  POST  -> license (content keys)
```

- The player is a Web SDK (`ATVWebPlayerSDK-*`); the device type is
  reported as a web player id (e.g. `AOAGZA014O5RE`).
- Media segments are **signed URLs** (token in query string) — like Max,
  the CDN does not check the cookie.
- The license POST is `text/plain` JSON containing four fields:
  `includeHdcpTestKey`, `playbackEnvelope`, `sessionHandoffToken`, and
  `licenseChallenge` (the CDM's own request).

### The wall: single-use, device-bound sessions

Unlike Max, Prime's playback sessions are **one-shot**:

1. A session (StartSession token, its `playbackEnvelope`, and the derived
   license tokens) is **consumed on first use**. Replaying any of it —
   including the browser's *own original* license request — returns
   `403 Denied`.
2. The session material is **bound to the device** that created it (the
   browser's Widevine CDM). An external L3 device's challenge against a
   fresh session is likewise `Denied`.

Consequences:

- A session cannot be replayed: the tokens are dead the moment the
  browser uses them.
- The working approach is **capture-assisted**: the browser performs the
  real playback (creating + consuming a live session), and a Widevine
  proxy (browser extension with a `.wvd` device) intercepts the EME flow
  so that *your* device generates the challenge and receives the license
  **inside the live session**. The extension can then hand you a
  ready-made download command (MPD + headers + keys).
- When the proxy runs in **WVD mode** (your `.wvd` is the CDM, not the
  browser's), the HAR of that playback carries the full license exchange.
  The keys can then be derived offline from the HAR alone, so a single
  HAR file is the complete input: MPD url + headers, keys, subtitles,
  and the content id.
- The signed MPD url stays valid for about 30 minutes; after that a
  fresh capture is needed.
- Subtitles are **not in the MPD**; they come from a separate
  `timedTextUrls` list (TTML) in the playback-resources response.

### Quality on Prime

- An L3 (browser) device is served up to **720p** (verified: 1280x720
  H.264 for a full-length film in the EMEA region). Public claims of a
  480p L3 ceiling were not observed in this region/account.
- The MPD also advertises 1080p representations; per the KID tier model
  (see [kid-tiers.md](kid-tiers.md)), the higher tiers will not yield keys
  to an L3 device.

### Operational notes

- Prime is more aggressive about unusual client behavior than Max. Keep
  volumes low (a few titles, human pacing) and use your own account.
- The capture-assisted flow needs: the Widevine proxy extension, your
  `.wvd` loaded into it, and a short real playback per title.

## General: what tends to break

| Symptom | Likely cause | Where to look |
|---|---|---|
| `playbackInfo` 400 | stale headers/body template | re-capture HAR, re-run init |
| license 403 / `Denied` | session consumed, or device mismatch | Prime: re-capture live; Max: check cert freshness |
| `key not found` in decrypt | KID tier mismatch | key aliasing (kid-tiers.md) |
| 480p instead of 720p | region/account tier policy | check what the license actually issued |
| CDN 403 mid-download | signed URL expired | re-resolve the item (fresh MPD) |
