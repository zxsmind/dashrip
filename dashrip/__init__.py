"""dashrip — DRM (Widevine) streaming automation framework.

dashrip downloads and decrypts DRM-protected streaming video and packages it
into MKV files with selectable audio tracks and embedded or sidecar
subtitles.

Supported platforms
-------------------
* **HBO Max** — fully automatic (session cookie + one-time request template).
* **Amazon Prime Video** — capture-assisted (a short browser capture supplies
  the signed manifest + keys; dashrip then downloads, decrypts and packages).

The package never bundles a Widevine device: you provide your own ``.wvd``
file (see ``docs/device.md``).
"""

__version__ = "0.1.1"
