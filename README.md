# gfx-capture

A small tethered-capture GUI for Fujifilm GFX / X-series cameras, written for photographing samples and devices in the lab. It uses the Fujifilm Camera Control SDK.

- Live view
- One-button capture (or Space), saving RAW and JPEG with the same name
- Pixel-shift multi-shot (16 RAW frames per capture), automatically merged into a full-colour 16-bit TIFF
- Shutter speed and ISO in manual mode
- File names from a template (`{sample}`, `{date}`, `{time}`, `{n}`) and a chosen save folder, with an optional subfolder per day

Tested with a GFX100S on Windows 10, SDK 1.34.

## Setup (Windows)

1. Install Python 3.10 or newer from python.org, including Tcl/Tk (it's on by default).
2. Download the **FUJIFILM X Series / GFX System Camera Control SDK** from Fujifilm and accept their license. Unzip it, for example to `C:\FujiSDK`. The SDK is not included here. Its license doesn't allow redistributing it.
3. Install gfx-capture:
   ```
   git clone https://github.com/sharpelab/gfx-capture C:\gfx-capture
   cd C:\gfx-capture
   py -m pip install -e .
   ```
4. On the camera, set **Setup → Connection Setting → PC Connection Mode → USB Tether Shooting Auto**, then connect it by USB.
5. Run `pyw -m gfx_capture`, or make a desktop shortcut to `pythonw.exe -m gfx_capture` with the start folder set to `C:\gfx-capture`.

gfx-capture searches `C:\FujiSDK` for `XAPI.dll`. If it isn't there, it asks for the folder, normally `...\REDISTRIBUTABLES\Windows\64bit`. You can also set the `GFX_SDK_DIR` environment variable.

Settings are kept in `%APPDATA%\gfx-capture\config.json`, and the log is in `%APPDATA%\gfx-capture\gfx-capture.log`.

## Notes

- **Warranty.** Fujifilm states that controlling a camera with the SDK voids the camera's warranty.
- The app runs the camera in *PC priority*. The camera's dials don't change exposure while it's connected; use the Exposure panel instead. *Auto* is the camera's Program mode.
- Live view pauses for each shot. Expect about 10 s per capture with a 100 MP body (transfer plus the camera's busy time).
- **Pixel shift** takes about 1.5 minutes and about 1.8 GB. The frames go into `<name>_pixelshift\`. Anything that moves during the 16 frames (vibration, drift) ruins the result.
  - gfx-capture then merges the first 4 frames (whole-pixel shifts) into `<name>_pixelshift_color.tif`: a 16-bit sRGB TIFF at the sensor's native resolution, with real R, G and B measured at every pixel instead of interpolated. A half-size `_preview.jpg` is saved too. This takes about 40 s on a modest PC. Brightness gets a fixed +1 stop and no contrast curve, so it's comparable between shots but looks a little flatter than the camera's JPEG.
  - To re-merge a folder: `python -m gfx_capture.pixelshift <folder>_pixelshift`
  - For the 400 MP version (which also uses the half-pixel frames), open the RAFs in Fujifilm's free *Pixel Shift Combiner*.
- RAW-only cameras give only a `.RAF`. Set the camera to RAW + JPEG if you want a quick-look JPEG.
- If the status says *busy* and doesn't recover, power-cycle the camera.
- Only USB is supported, and only one camera at a time.

## Not affiliated with Fujifilm

FUJIFILM, GFX and X Series are trademarks of FUJIFILM Corporation. This project is an independent application that calls the SDK the user installs. It contains no Fujifilm code, headers, libraries or documentation.

## License

MIT (this repository's code only).
