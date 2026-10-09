# dashrip

Rip DRM-protected (Widevine) streaming video from HBO Max and Amazon Prime
Video into local MKV files. Pick the audio and subtitle tracks you want, add
posters, and file everything into a standard media library layout.

## Legal

dashrip decrypts content delivered under a DRM license and a service
agreement. Using it may breach a streaming service's terms of service, and
circumventing technological protection measures is restricted by law in some
jurisdictions.

- Use it only with content you are entitled to access, such as your own
  active subscription.
- You are solely responsible for your use and any consequences, including
  action against your account by the service.
- It is provided for educational purposes and for interoperability with your
  own licensed content. No warranty of any kind.

If you are unsure whether your use is legal where you live, ask a lawyer.

## What it does

One command runs the whole pipeline: resolve the title, fetch the DASH
manifest, request the Widevine license, download the media, decrypt it, remux
to MKV, and file it into your library.

- Audio and subtitle selection from the tracks that exist for that item.
- A standard library layout (Jellyfin/Plex style):

  ```
  <out>/Movies/<Title> (<Year>)/<Title>.720p.mkv
  <out>/Series/<Show>/Season NN/Episode NN - <EpTitle>/SxxEyy - <EpTitle>.720p.mkv
  <out>/Series/<Show>/poster.jpg
  ```

- Show posters and per-episode artwork from the platform's own CMS.
- A JSON manifest recording each delivered file: path, size, SHA-256,
  duration, tracks, date.
- Batch mode with resumable downloads.
- `doctor`: a health check of the whole stack before you start.
- `sync`: a resumable, SHA-256-verified folder transfer for moving a finished
  library to a server or NAS.
- Defaults for the track prompts (`defaults` in `config/dashrip.json`): the
  choices applied when a prompt is left blank. The `orig` pseudo-code stands
  for the content's original language; for example
  `"audio": ["orig", "tr"]` keeps the original track plus a Turkish dub when
  one exists.

## Requirements

- Python 3.10+
- `pywidevine`, `requests` (`pip install -r requirements.txt`)
- `ffmpeg` / `ffprobe`
- `shaka-packager`
- A Widevine device file (`.wvd`), see below
- A valid session cookie
- One HAR capture exported from your browser (for the playback template)

### The `.wvd` file

A `.wvd` file is a Widevine device: the CDM key material plus a device
identity. dashrip does not ship one and will not run without your own. It is
a credential. Obtain it lawfully for a device you own, keep it secure, and
note that it is never uploaded anywhere by this software.

Devices at L3 security level typically top out at 720p on these platforms;
L1 devices unlock higher tiers. See [docs/kid-tiers.md](docs/kid-tiers.md).

## Quickstart

```bash
git clone https://github.com/zxsmind/dashrip
cd dashrip
pip install -r requirements.txt

python -m dashrip init      # one-time guided setup
python -m dashrip doctor    # confirm everything is green
python -m dashrip max "interstellar"
```

`init` asks for the device file, the session cookie, a HAR capture (F12,
Network, play anything, "Save all as HAR"), your ffmpeg and shaka paths, and
the output directory, then runs `doctor`.

### `max`

Interactive:

```
$ python -m dashrip max
> the dark knight          (title, URL, or show UUID)
> .
Output directory [default]:
  video: 1280x534 | audio: en-US, tr | subs: en-US, tr
  which audio to include? (blank = all)
  which subs to include?  (blank = all, n = none)
  embed in MKV (1) or sidecar files (2)?
  download show poster? [Y/n]:
```

Paste several titles before the final `.` to batch them.

Non-interactive:

```bash
python -m dashrip max "inception" "tenet" \
    --audio tr,en --subs tr,en --embed 1 --poster yes \
    --outdir D:/Media
```

Series episodes: `--ep 1-6`, `--ep S01E02`, and so on.

### `prime` (capture-assisted)

Amazon's player uses single-use, device-bound playback sessions, so a fully
headless pipeline is not possible. One short browser playback is the input;
everything after that is automatic.

HAR flow (no pasting, no typing):

1. In the browser, with a Widevine proxy extension loaded with your `.wvd`
   in WVD mode, play the content for about 10 seconds.
2. Save the DevTools network capture: right click -> "Save all as HAR with
   content".
3. Run `python -m dashrip prime capture.har`.

The HAR carries the signed MPD url with its request headers, the Widevine
license exchange (the keys are derived from it with your device), the
subtitle tracks, and the content id. The title, year, and poster are
resolved from Prime's public detail page.

Paste-a-command flow: same capture, but copy the ready-made N_m3u8DL-RE
command from the extension's History and paste it when asked. Offer the
HAR as well to keep subtitles and the automatic title/year. The HAR yields
the keys only when the proxy ran in WVD mode with the device from your
config; otherwise the keys come from the pasted command.

MPD urls expire after about 30 minutes; recapture for another run.

dashrip then downloads, decrypts, verifies, files, and records the result.
If a Turkish dub exists for the content and it is not the track you selected,
it is fetched and added as an extra audio track (no re-encode).
See [docs/platform-notes.md](docs/platform-notes.md).

## How it works

1. The platform CMS resolves the title to show and video IDs.
2. A playbackInfo request (your captured template plus your cookie) returns
   the signed DASH manifest URL and the Widevine license URL.
3. The MPD is parsed: the best video representation, every audio dub, every
   subtitle track, and the Widevine PSSH.
4. Your `.wvd` device requests a license; the content keys are extracted,
   with per-tier key-id aliasing. See [docs/kid-tiers.md](docs/kid-tiers.md)
   for why this step is required.
5. The media files are downloaded from the CDN, decrypted with
   shaka-packager, and remuxed to MKV with ffmpeg.
6. The result is moved into the library layout, posters are fetched, and the
   manifest is updated. Working files are removed.

Module layout and design notes: [docs/architecture.md](docs/architecture.md).
Per-platform behavior: [docs/platform-notes.md](docs/platform-notes.md).

## Troubleshooting

Run `python -m dashrip doctor`. It reports, line by line, which piece of the
stack is missing or rejected:

- `device ... not set`: load a `.wvd` in `init`.
- `cookie ... EXPIRED`: capture a fresh cookie (F12, Network, any request,
  request headers, `cookie`).
- `cms ... HTTP 401/403`: the cookie or session headers in the config are
  stale. Recapture the HAR and run `init` again.
- `playback ... missing`: your HAR had no playbackInfo request. Play
  something in the browser before saving the HAR.

## Security

Your cookie, `.wvd`, and the playback template live in `config/dashrip.json`,
which is git-ignored. Treat that file like a password. Media is downloaded
directly from the platform CDN over HTTPS. Nothing is sent to any third-party
service, and dashrip never uploads your media, device, or credentials.

## Contributing

Keep platform-specific quirks in `core.py`, keep the CLI thin, and add an
offline smoke test for anything you change. The development tree has
`test_dashrip_*.py`.

## License

MIT. See [LICENSE](LICENSE). The legal notice above still applies: you use
this at your own responsibility.
