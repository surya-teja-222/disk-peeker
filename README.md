# SSD Browser

A local, Finder-style browser for an unmounted NTFS SSD on macOS. Browse folders, preview photos and videos, and download original files through your web browser.

The application reads the selected NTFS partition directly. It does not create an operating-system mount or write to the SSD. Administrator access to the raw disk device is required; device access restrictions can still prevent it from working.

## Features

- Grid and list views with adjustable thumbnail sizes.
- Folder breadcrumbs, navigation, search within the current folder, and sorting.
- On-demand image and video thumbnails, with a bounded in-memory cache.
- Quick Look image previews and browser-supported video playback with seeking.
- Original-file downloads streamed without staging a full copy.
- Localhost-only server with a new access token each time it starts.

## Files

```text
ssd-browser/
├── README.md
├── ssd_raw_browser.py
└── ssd_browser_ui.html
```

Keep the Python and HTML files in the same directory. The Python server loads its interface from the neighboring HTML file. There is no frontend build step or Python package installation.

## Requirements

- macOS and Python 3.
- `ntfsls`, `ntfscat`, and `libntfs-3g` from `ntfs-3g-mac`.
- FFmpeg for image and video thumbnails.
- macOS `sips`, included with the OS, for additional image format support.

The implementation was tested on an Apple Silicon Mac with these Homebrew packages already installed. To install them on another Mac with Homebrew:

```bash
brew install python ffmpeg gromgit/fuse/ntfs-3g-mac
```

The server searches the executable PATH and the Apple Silicon Homebrew locations for utilities. For the native library, it checks `/opt/homebrew/lib/libntfs-3g.dylib` and `/usr/local/lib/libntfs-3g.dylib`.

## Run

1. Find the SSD's NTFS partition:

   ```bash
   diskutil list external physical
   ```

   Use the partition identifier, such as `disk8s1`, rather than the whole-disk identifier `disk8`. Identifiers can change after reconnecting a drive.

2. Change into this folder and start the server, substituting your partition:

   ```bash
   sudo "$(command -v python3)" ssd_raw_browser.py --device /dev/disk8s1
   ```

3. Enter your Mac password in Terminal. Open the complete URL printed by the server, including its `token` query parameter.

4. Press **Ctrl+C** in Terminal to stop. Restarting generates a new URL/token.

To choose another port:

```bash
sudo "$(command -v python3)" ssd_raw_browser.py --device /dev/disk8s1 --port 8766
```

The default port is `8765`. The server binds to `127.0.0.1`, so it is accessible from the same Mac.

## Using the browser

- **Single-click** to select an item.
- **Double-click** a folder to enter it, or a photo/video to preview it.
- **Space** previews a focused media item; **Enter** opens a focused item.
- Use the hover download button or **Download original** in the preview window to save a file.
- Use **Escape** to close a preview and the arrow buttons to move between media items.
- Use **⌘K** to focus search. Search and the Photos/Videos filters apply to the current folder.
- Use the view buttons for grid/list mode and the bottom slider to resize grid thumbnails.

## Media behavior and limitations

Thumbnails are generated lazily and cached in RAM, up to approximately 64 MB. The native NTFS reader supports byte-range requests, allowing video seeking and thumbnail extraction without copying entire videos first.

HEIC and camera RAW previews depend on available decoders. If FFmpeg cannot decode an image, the server tries macOS ImageIO through `sips` for images up to 128 MB; this fallback temporarily copies that image to the Mac and removes the temporary files afterward. Some formats will show only a file icon.

Video playback depends on the browser's container and codec support. A video may have a thumbnail but still require downloading the original to play in another application. Files remain downloadable even when preview generation fails.

Directory listings are cached for 30 seconds; the refresh button requests a fresh listing. Browsing is intended for a connected SSD whose contents are not being modified concurrently. Folder downloads, recursive search, file uploads, renaming, deletion, and editing are not implemented.

If the native NTFS library cannot initialize, the server retains basic downloads through `ntfscat`, with previews disabled.

## Troubleshooting

- **Permission denied:** run with `sudo` and confirm that device access is allowed on the Mac.
- **Cannot read SSD:** check the partition identifier and that the SSD is connected and contains an NTFS filesystem.
- **Address already in use:** stop the previous server with Ctrl+C or choose another `--port`.
- **403 / complete URL required:** open the latest URL printed in Terminal, including its token.
- **Missing interface file:** keep `ssd_browser_ui.html` beside `ssd_raw_browser.py`.
- **Missing previews:** check the FFmpeg installation and native library paths. Unsupported media can still be downloaded.

## Validation

The server was checked against a temporary NTFS image using real NTFS utilities and the native library. Checks covered byte-offset reads, image/video thumbnails, full downloads, empty files, HTTP ranges, authentication, and Unicode filenames. Browser checks covered grid/list views, folder navigation, filtering/search, image previews, video playback/seeking, and desktop/narrow layouts.
