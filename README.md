# dashrip

Rip DRM-protected (Widevine) streaming video to local MKV files — with
audio tracks, subtitles, posters, and a standard media-library layout.

**Supported platforms:** HBO Max · Amazon Prime Video

---

## ⚖️ Legal notice — read this first

dashrip decrypts content that is delivered under a DRM license and a
service agreement. Using this software may violate the terms of service of
the streaming services involved, and in some jurisdictions the
circumvention of technological protection measures may be restricted by
law.

- **Use this software only with content you are entitled to access** (e.g.
  your own subscription, while it is active).
- **You are solely responsible** for how you use this software and for any
  consequences that follow, including account actions by the service
  provider.
- The authors provide dashrip **for educational purposes and as a tool for
  interoperability with your own licensed content**. No warranty of any
  kind is given. By using it you accept full responsibility.

If you are unsure whether your use is legal where you live, ask a lawyer.

---

## What it does

`dashrip` automates the full pipeline that used to be several manual
tools:

```
resolve content → fetch manifest (DASH MPD) → Widevine license (L3/L1 CDM)
→ download media segments → decrypt (shaka-packager) → remux to MKV
→ file into a standard library layout → record in a JSON manifest
```

Features:

- **Dynamic track detection** — the actual audio dubs and subtitle tracks
  that exist for *that specific item* are read from the manifest and
  offered for selection (nothing is hard-coded).
- **Standard library layout**
  (Jellyfin/Plex-style):
  ```
  <out>/Movies/<Title> (<Year>)/<Title>.720p.mkv
  <out>/Series/<Show>/Season NN/Episode NN - <EpTitle>/SxxEyy - <EpTitle>.720p.mkv
  <out>/Series/<Show>/poster.jpg
  ```
- **Posters** — portrait show cards (with title) and per-episode artwork,
  pulled from the platform's own CMS.
- **Manifest** — every delivered file is recorded with path, size,
  SHA-256, duration, tracks, and date (`dashrip_manifest.json`).
- **Batch mode** — many titles in one run; resume-capable downloads.
- **Human-optional pacing** — no aggressive parallelism by default;
  optional speed cap.
- **`doctor`** — a green/red health check of the entire stack before you
  start.
- **`sync`** — a lossless, resumable, SHA-256-verified folder transfer
  helper (useful for moving a finished library to a server/NAS).

## Requirements

| Component | Why |
|---|---|
| Python 3.10+ | the code |
| `pywidevine`, `requests` | Widevine device handling, HTTP (`pip install -r requirements.txt`) |
| `ffmpeg` / `ffprobe` | muxing, verification |
| `shaka-packager` | CENC/CEA-708 decryption into MKV |
| A **Widevine device file** (`.wvd`) | the CDM key material — see below |
| A valid session **cookie** | your account's session token |
| An exported **HAR** from your browser | the playback request template (once) |

### About the `.wvd` file

A `.wvd` file is a Widevine *device* (key + identity). dashrip does **not**
ship one and will not work without your own. You are responsible for
obtaining one lawfully for a device you own, and for keeping it secure —
**it is a credential**. It is never uploaded anywhere by this software.

> Practical note: devices at **L3** security level typically unlock up to
> 720p on these platforms; **L1** devices unlock higher tiers. See
> [docs/kid-tiers.md](docs/kid-tiers.md).

## Quickstart

```bash
git clone https://github.com/<you>/dashrip
cd dashrip
pip install -r requirements.txt

python -m dashrip init      # guided one-time setup
python -m dashrip doctor    # verify everything is green
python -m dashrip max "interstellar"
```

`init` asks for the device file, the session cookie, a HAR capture from
your browser (F12 → Network → play anything → *Save all as HAR*), your
ffmpeg/shaka paths, and the output directory — then runs `doctor`.

### `max` — interactive

```
$ python -m dashrip max
> the dark knight          (or a URL, or a show UUID)
> .
Output directory [default]:
  video: 1280x534 | audio: en-US, tr | subs: en-US, tr
  which audio to include? (blank = all)
  which subs to include?  (blank = all; n = none)
  embed in MKV (1) / sidecar files (2)?
  download show poster? [Y/n]:
```

Batch: paste several lines before the final `.`.

### `max` — non-interactive / flags

```bash
python -m dashrip max "inception" "tenet" \
    --audio tr,en --subs tr,en --embed 1 --poster yes \
    --outdir D:/Media
```

Series episode selection: `--ep 1-6`, `--ep S01E02`, etc.

### `prime` — Amazon Prime (capture-assisted)

Amazon's player uses **single-use, device-bound playback sessions**, so a
fully headless pipeline is not possible. The supported flow is
capture-assisted:

1. In the browser (with a Widevine proxy extension + your `.wvd` loaded),
   play the content for ~10 seconds.
2. Copy the ready-made `N_m3u8DL-RE` command from the extension's History.
3. `python -m dashrip prime` and paste it.

dashrip then downloads, decrypts, verifies, renames, files, and records
the result automatically. See [docs/platform-notes.md](docs/platform-notes.md).

## How it works (short version)

1. The platform's CMS resolves the title to show/video IDs.
2. A `playbackInfo` request (your captured template + your cookie) returns
   the signed DASH manifest URL and the Widevine license URL.
3. The MPD is parsed: the best available video representation, every audio
   dub, every subtitle track, and the Widevine PSSH.
4. The `.wvd` device requests a license; the content keys are extracted
   (with per-tier key-ID aliasing — see the docs, this matters).
5. Media files are downloaded from the CDN (plain HTTP, token in URL),
   decrypted with shaka-packager, and remuxed to MKV with ffmpeg.
6. The result is moved into the standard layout, poster(s) fetched, and
   the manifest updated. Transient working files are removed.

Deep dives: [docs/architecture.md](docs/architecture.md) ·
[docs/kid-tiers.md](docs/kid-tiers.md) ·
[docs/platform-notes.md](docs/platform-notes.md)

## Troubleshooting

Run `python -m dashrip doctor` — it reports, line by line, which piece of
the stack is missing or rejected:

- `device ... not set` → load a `.wvd` in `init`.
- `cookie ... EXPIRED` → capture a fresh cookie (F12 → Network → any
  request → request headers → `cookie`).
- `cms ... HTTP 401/403` → cookie or the session headers in the config are
  stale; re-capture the HAR and run `init` again.
- `playback ... missing` → your HAR did not include a `playbackInfo`
  request; play something in the browser *before* saving the HAR.

## Security & privacy notes

- Your cookie, `.wvd`, and the playback template live in
  `config/dashrip.json` (git-ignored). Treat that file like a password.
- Media is downloaded from the platform's CDN over HTTPS; no third-party
  service is involved.
- dashrip never uploads your media, device, or credentials anywhere.

## Contributing

PRs are welcome. Keep the platform-specific quirks in `core.py` /
`platform notes`, keep the CLI thin, and add an offline smoke test for
anything you change (see `test_dashrip_*.py` in the development tree).

## License

MIT — see [LICENSE](LICENSE). And the legal notice above stands: you use
this at your own responsibility.
