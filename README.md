# dashrip

[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

Rip DRM-protected (Widevine) streaming video from **HBO Max** and **Amazon
Prime Video** into local MKV files. One command runs the whole pipeline:
resolve the title, fetch the DASH manifest, request the license, download and
decrypt the media, remux with the audio and subtitle tracks you pick, and file
the result into a Jellyfin/Plex-style library with posters.

```
$ python -m dashrip max "interstellar"
[dashrip] === [1/1] interstellar ===
[dashrip]   show: Interstellar
[dashrip]   video: 1280x534 | audio: en-US, en-GB | subs: en-US, de
  which audio to include? (blank = en-US; or comma list)
  which subs to include?  (blank = all; n = none)
  Subtitles: embed in MKV (1) / separate sidecar files (2) [1]:
[dashrip]   plan: 1 item | audio: ['en-US'] | subs: ['en-US', 'de'] | mode: merge
[dashrip]   [1/1] done: D:/Media/Movies/Interstellar (2014)/Interstellar.720p.mkv
```

> dashrip ships no Widevine device. You supply your own `.wvd` file (see
> [docs/device.md](docs/device.md)); it never leaves your machine.

## What it does

- **One command per title.** No manual manifest, key, or ffmpeg plumbing.
- **Media-library output.** Files land in the standard
  `Movies/<Title> (<Year>)/` and `Series/<Show>/Season NN/…` layout, with
  show posters and per-episode artwork pulled from the platform.
- **Pick your tracks.** Every audio dub and subtitle track that exists for
  the item is listed; choose what to keep, embed or sidecar.
- **A manifest, not a mystery.** Every delivered file is recorded in
  `dashrip_manifest.json` with path, size, SHA-256, duration, tracks, date.
- **Resumable.** Interrupted downloads continue from where they stopped;
  batch runs survive a failed item and report a summary at the end.
- **`doctor`.** A green/red check of the whole stack (device, cookie, tools,
  cookie expiry, CMS reachability) before you spend time on a download.
- **`sync`.** A resumable, SHA-256-verified folder transfer for moving a
  finished library to a server or NAS.

## How it compares

The closest tools are [devine](https://github.com/devine-dl/devine) (a broad,
extractor-per-service downloader) and
[N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE) (a fast downloader that
decrypts given keys). dashrip is deliberately narrower: two platforms, one
command, and the whole path from title to a library-ready MKV — including
obtaining the keys, which N_m3u8DL-RE leaves to you and devine structures
per-service. If you want one tool for many services, devine is the wider
choice; if you want HBO Max and Prime filed into a media library with
posters and a manifest, that is exactly what dashrip is for.

## Requirements

- Python 3.10+
- `pywidevine`, `requests` — `python -m pip install -r requirements.txt`
- `ffmpeg` / `ffprobe` — <https://ffmpeg.org/download.html>
- `shaka-packager` — <https://github.com/shaka-project/shaka-packager/releases>
- A Widevine device file (`.wvd`) — see [docs/device.md](docs/device.md)
- A platform session cookie (HBO Max)
- One HAR capture exported from your browser (for the playback template)

### The `.wvd` file

A `.wvd` file is a Widevine device: the CDM key material plus a device
identity. dashrip does not ship one and will not run without your own. It is a
credential — obtain it lawfully for a device you own, keep it secure, and note
that it is never uploaded anywhere by this software.

Devices at L3 security level typically top out at 720p on these platforms; L1
devices unlock higher tiers. See [docs/kid-tiers.md](docs/kid-tiers.md).

## Install

```bash
git clone https://github.com/zxsmind/dashrip
cd dashrip
python -m pip install -r requirements.txt

python -m dashrip init      # one-time guided setup
python -m dashrip doctor    # confirm everything is green
python -m dashrip max "interstellar"
```

`init` asks for the device file, the session cookie, a HAR capture (F12 →
Network → play anything → "Save all as HAR"), your ffmpeg and shaka paths, and
the output directory, then runs `doctor`.

## Commands

### `max` — HBO Max (fully automatic)

Interactive:

```bash
python -m dashrip max
```

Paste several titles before the final `.` to batch them.

Non-interactive:

```bash
python -m dashrip max "inception" "tenet" \
    --audio en,de --subs en,de --embed 1 --poster yes \
    --outdir D:/Media
```

Series episodes: `--ep 1-6`, `--ep S01E02`, and so on.

### `prime` — Amazon Prime Video (capture-assisted)

Amazon's player uses single-use, device-bound playback sessions, so a fully
headless pipeline is not possible. One short browser playback is the input;
everything after that is automatic.

1. In the browser, with a Widevine proxy extension loaded with your `.wvd` in
   WVD mode, play the content for about 10 seconds.
2. Save the DevTools capture: right-click → "Save all as HAR with content".
3. Run `python -m dashrip prime capture.har`.

The HAR carries the signed MPD url with its request headers, the Widevine
license exchange (the keys are derived from it with your device), the subtitle
tracks, and the content id. The title, year, and poster are resolved from
Prime's public detail page.

MPD urls expire after about 30 minutes; recapture for another run. Optional
`extra_audio` in the config (a list of language codes, empty by default): for
each code, a matching audio track that was not the selected one is also
embedded as an extra track (no re-encode). See
[docs/platform-notes.md](docs/platform-notes.md).

### `doctor` / `init` / `sync`

```bash
python -m dashrip doctor                     # health check
python -m dashrip init                       # (re)capture setup
python -m dashrip sync sync   --src C:\Media --target /srv/media \
    --host 203.0.113.10 --user media --key key.pem
python -m dashrip sync status --src C:\Media --target /srv/media --host … --user … --key …
python -m dashrip sync verify --src C:\Media --target /srv/media --host … --user … --key …
```

## Output layout

```
<out>/Movies/<Title> (<Year>)/<Title>.720p.mkv
<out>/Series/<Show>/Season NN/Episode NN - <EpTitle>/SxxEyy - <EpTitle>.720p.mkv
<out>/Series/<Show>/poster.jpg
```

## How it works

1. The platform CMS resolves the title to show and video IDs.
2. A playbackInfo request (your captured template plus your cookie) returns the
   signed DASH manifest URL and the Widevine license URL.
3. The MPD is parsed: the best video representation, every audio dub, every
   subtitle track, and the Widevine PSSH.
4. Your `.wvd` device requests a license; the content keys are extracted, with
   per-tier key-id aliasing. See [docs/kid-tiers.md](docs/kid-tiers.md) for why
   this step is required.
5. The media files are downloaded from the CDN, decrypted with shaka-packager,
   and remuxed to MKV with ffmpeg.
6. The result is moved into the library layout, posters are fetched, and the
   manifest is updated. Working files are removed.

Module layout and design notes: [docs/architecture.md](docs/architecture.md).
Per-platform behavior: [docs/platform-notes.md](docs/platform-notes.md).

## Configuration

Everything lives in `config/dashrip.json` (git-ignored, created by `init`,
editable by hand):

| Key | Meaning |
|---|---|
| `wvd` | path to your Widevine device file |
| `cookie` | platform session cookie |
| `ffmpeg` / `ffprobe` / `shaka` | tool paths (or bare command names) |
| `outdir` | default output directory |
| `bwlimit_kbps` | download speed cap in kbit/s (0 = unlimited) |
| `extra_audio` | extra audio language codes to embed in the Prime flow |
| `playback` | captured playbackInfo template (url, headers, body, edit id) |
| `cms_headers` | captured CMS request headers |
| `defaults` | track prompt defaults; `orig` means the content's original language, e.g. `"audio": ["orig", "en"]` |

## Troubleshooting

Run `python -m dashrip doctor`. It reports, line by line, which piece of the
stack is missing or rejected:

- `device ... not set`: load a `.wvd` in `init`.
- `cookie ... EXPIRED`: capture a fresh cookie (F12 → Network → any request →
  request headers → `cookie`).
- `cms ... HTTP 401/403`: the cookie or session headers in the config are
  stale. Recapture the HAR and run `init` again.
- `playback ... missing`: your HAR had no playbackInfo request. Play something
  in the browser before saving the HAR.

More failure modes and their causes: [docs/platform-notes.md](docs/platform-notes.md).

## Contributing

- Keep platform-specific quirks in `core.py`, keep the CLI thin, and keep
  `structure.py` dependency-free.
- Add or update an offline test in `tests/` for anything you change. The suite
  needs no network, device file, or media:

  ```bash
  python -m pip install requests pytest
  python -m pytest -q
  ```

  CI runs it on Python 3.10–3.13.

## Security

Your cookie, `.wvd`, and the playback template live in `config/dashrip.json`,
which is git-ignored. Treat that file like a password. Media is downloaded
directly from the platform CDN over HTTPS. Nothing is sent to any third-party
service, and dashrip never uploads your media, device, or credentials.

## Legal

dashrip decrypts content delivered under a DRM license and a service agreement.
Using it may breach a streaming service's terms of service, and circumventing
technological protection measures is restricted by law in some jurisdictions.

- Use it only with content you are entitled to access, such as your own active
  subscription.
- You are solely responsible for your use and any consequences, including
  action against your account by the service.
- It is provided for educational purposes and for interoperability with your
  own licensed content. No warranty of any kind.

If you are unsure whether your use is legal where you live, ask a lawyer.

## License

MIT. See [LICENSE](LICENSE). The legal notice above still applies: you use this
at your own responsibility.
