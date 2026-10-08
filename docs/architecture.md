# Architecture

## Module map

```
dashrip/
  __main__.py     entry point (python -m dashrip)
  cli.py          command dispatch + interactive flows (init/max/prime/sync)
  doctor.py       green/red health check
  config.py       config file handling (JSON), cookie/JWT helpers, tool resolution
  core.py         platform pipeline: CMS, playbackInfo, MPD, keys, CDN, decrypt, mux
  structure.py    output layout, naming, manifest, cleanup (stdlib only)
  sync.py         lossless resumable scp folder transfer
```

Design rules:

- `core.py` owns **everything platform-specific** (endpoints, header
  shapes, manifest structure, key handling). Adding a platform means a new
  set of functions here, not a rewrite.
- `structure.py` is **dependency-free** (standard library only) so the
  layout/manifest logic can be tested and reused without pulling in the
  network/DRM stack.
- `cli.py` is thin: it turns user input into `core`/`structure` calls. No
  HTTP and no DRM in the CLI.

## Pipeline (HBO Max)

```
                 ┌────────────┐
  title/URL/UUID │  CMS route │  GET /cms/routes/show/{id}
  ──────────────►│  resolve   │  cookie + session headers
                 └─────┬──────┘
                       │  videos: {editId, vid, name, season, episode, ...}
                       ▼
                 ┌────────────┐
                 │ playback   │  POST /playbackInfo   (captured template)
                 │  request   │  body: captured request with editId swapped
                 └─────┬──────┘
                       │  mpd_url (signed) · license_url · service_cert
                       ▼
                 ┌────────────┐
                 │  MPD parse │  ElementTree:
                 │            │  - best video rep (largest w*h, relative BaseURL)
                 │            │  - all audio dubs (per lang)
                 │            │  - all subtitle tracks (SegmentTemplate $Number$)
                 │            │  - Widevine PSSH (cenc-namespaced)
                 └─────┬──────┘
                       ▼
                 ┌────────────┐
                 │  license   │  .wvd device → license request →
                 │  + keys    │  license response → content keys (CEKs)
                 └─────┬──────┘     (per-key KID tier aliasing — see kid-tiers.md)
                       ▼
        ┌──────────────────────────────────────────┐
        │ download (CDN, plain HTTP + token in URL) │  video · each audio dub ·
        │  resume-capable, optional speed cap        │  subtitle segments
        └───────────────────┬──────────────────────┘
                            ▼
                 ┌────────────┐
                 │ decrypt    │  shaka-packager, raw-key decryption
                 │            │  (CENC/subsample; one .dec.mp4 per track)
                 └─────┬──────┘
                       ▼
                 ┌────────────┐
                 │  remux     │  ffmpeg -c copy → MKV
                 │  + verify  │  ffprobe + 15 s quick-decode check
                 └─────┬──────┘
                       ▼
                 ┌────────────┐
                 │  deliver   │  standard layout + poster(s) +
                 │            │  manifest entry + working-dir cleanup
                 └────────────┘
```

## Key decisions

### 1. One file per track (Max's content model)

Max serves each content track as **one complete MP4** (SegmentBase with a
single BaseURL per representation), not a segment ladder. The MPD, however,
is **SSAI-shaped**: ad/bumper periods appear alongside the content period.
Ad periods use *explicit external* BaseURLs; the content period's video
representations use *relative* BaseURLs. `core.parse_mpd` selects content
periods exactly by that rule (relative video BaseURL + audio present),
which also filters out the ad periods.

### 2. Service certificate is dynamic

The Widevine service certificate is **not static** — it is returned inside
every `playbackInfo` response
(`drm.schemes.widevine.widevineServiceCertificate`). The pipeline always
uses the certificate from the *current* response, never a saved copy.

### 3. Keys are aliased across KID tiers

The license returns content keys, but the MPD's per-track `default_KID`
may be expressed in a different tier (see
[kid-tiers.md](kid-tiers.md)). `core.keys_arg` registers every real key
under all six tier variants so the decryptor always finds a match. This is
a bug-magnet if forgotten: with a HW-tier KID in the MPD and a SW key from
the license, shaka-packager fails with "Key for key_id=... was not found"
until aliasing is applied.

### 4. Subtitles are global-timestamp VTT

Max's subtitle segments carry **global 0-based cue timestamps** (no
per-segment offset), so merging is a concatenation + sequential
renumbering — no rebasing. Each VTT block may contain multiple cues
(separated by blank lines); the merger parses per-cue, not per-block.

### 5. Verification before delivery

A finished MKV is only "delivered" after:

1. `ffprobe` succeeds and reports a sane duration;
2. a 15-second `ffmpeg -v error` decode pass is silent.

If the decode pass reports errors, the item is still delivered but marked
`OK (decode warning!)` in the run summary — the source encode itself can
be damaged upstream (this has happened), and the warning tells you which
files to re-verify.

### 6. Transient vs final state

Everything that is downloaded/decrypted lives in a private working
directory (`~/.dashrip/work/.<name>/`). Only after successful mux + verify
is the file moved into the final library location, the manifest updated,
and the working directory removed. A killed run therefore never leaves
half-files in the library.

## Error model

- **Network hiccups** during segment download: the download is
  resume-capable (`.part` + `Range`); a re-run continues.
- **License rejection**: fails fast with the HTTP status; the run summary
  lists the item as `ERROR`.
- **Partial failure in a batch**: one item failing never aborts the rest;
  results are aggregated in a final summary.

## The sync module

`sync` is intentionally a separate concern (moving a finished library),
but shares the same philosophy: **verify everything**. Every file is
SHA-256-hashed locally, compared to the remote state, copied with scp,
and re-hashed remotely. The checkpoint (per src/target pair) makes any
run — after sleep, network drop, or corruption — safe to simply re-run.
