# Resolve PowerGrade Automation

## Compatibility

The current Windows acceptance run used DaVinci Resolve Studio 21.2.0.6. This is a
compatibility note, not a requirement. Resolve scripting support and the documented
API methods still need to be present in the installed version.

## Runtime Environment

For a visible Resolve instance, set external scripting to Local. Automated batches can
run with:

```powershell
Resolve.exe -nogui
```

Script locations are discovered by `resolve_client.py` in this order:

1. Explicit environment overrides.
2. Platform-standard Resolve installation paths.
3. Existing executable or library candidates.

Windows checks the Resolve API under `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve`
and checks `fusionscript.dll` and `Resolve.exe` under the standard Program Files
installation layout. macOS checks `/Library/Application Support/Blackmagic Design` and
the standard `/Applications/DaVinci Resolve` application bundle. Linux checks
`/opt/resolve`. Non-standard installations must provide the overrides below.

## Resolve Environment

| Variable | Meaning |
| --- | --- |
| `RESOLVE_SCRIPT_API` | Full path to the Resolve `Scripting` directory containing `Modules`. |
| `RESOLVE_SCRIPT_LIB` | Full path to `fusionscript.dll`, `fusionscript.dylib` or `fusionscript.so`. |
| `RESOLVE_EXECUTABLE` | Full path to the Resolve executable. |
| `RESOLVE_LUT_DIR` | Exact destination directory for the Xiaomi LUT set. |
| `RESOLVE_LUT_ROOT` | Legacy parent directory; `Xiaomi15U_ResolveLab` is appended. |
| `RESOLVE_LIVE_TESTS` | Set to `1` to opt into local Resolve integration tests. |

`RESOLVE_LUT_DIR` takes precedence over `RESOLVE_LUT_ROOT`. Missing Resolve paths
disable only the Resolve route; ffmpeg analysis and fallback delivery remain available.

## Project Settings

The batch project uses:

```text
3840 x 2160
60 fps timeline
DaVinci YRGB
Rec.709 Scene timeline
Rec.709 Scene output
```

The Xiaomi source is not treated as a standard camera log. The verified source LUT is
applied inside node 1.

## PowerGrade Contract

Every runtime DRX must contain six serial nodes:

```text
01 SOURCE
02 SHOT BALANCE
03 TONE MAP
04 SKIN PROTECT
05 LOOK
06 OUTPUT TRIM
```

`resolve_powergrades.validate_graph()` checks node count, labels and the source LUT.
The runtime templates currently express tone, skin protection and output trim as
deterministic LUT stages inside Resolve. This keeps the template reproducible in
headless mode while preserving the six-stage Resolve node graph.

## Per-Shot CDL

CDL is written to node 2:

```python
item.SetCDL(
    {
        "NodeIndex": "2",
        "Slope": "1.000 1.000 1.000",
        "Offset": "0.000 0.000 0.000",
        "Power": "1.000 1.000 1.000",
        "Saturation": "1.000",
    }
)
```

Limits:

- Slope: `0.90` to `1.10`
- Offset: `-0.04` to `0.04`
- Power: `0.92` to `1.08`
- Saturation: `0.92` to `1.08`

The CDL is shot balancing, not the Look. Never bake a different look into node 2.

## Preview Render

Resolve preview jobs use:

- MP4 H.264
- 1920x1080
- 150 frames or the clip end
- Resolve render queue

ffmpeg may probe and extract still frames from the Resolve preview; it must not
generate the preview video.

## Final Render

Final jobs use:

- MP4
- H.265 software codec
- `EncodingProfile = Main10`
- 3840x2160
- 60 fps
- audio enabled
- H.265 Master preset

The QC gate requires:

```text
profile=Main 10
pix_fmt=yuv420p10le
color_range=tv
color_space=bt709
color_transfer=bt709
color_primaries=bt709
```

## LUT Installation

Use `RESOLVE_LUT_DIR` when Resolve uses a custom LUT location. Otherwise the installer
selects the platform directory:

- Windows: prefer
  `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\LUT\Xiaomi15U_ResolveLab`
  when the parent exists; otherwise use the Roaming AppData Resolve LUT directory.
- macOS:
  `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Support/LUT/Xiaomi15U_ResolveLab`.
- Linux:
  `~/.local/share/DaVinciResolve/Support/LUT/Xiaomi15U_ResolveLab`.

`Project.RefreshLUTList()` is required after copying LUTs. If Resolve does not discover
the files immediately, restart Resolve before running the batch.

## Failure Recovery

- Resolve connection fails: verify Resolve is running and Local scripting is enabled.
- Discovery fails: set `RESOLVE_SCRIPT_API`, `RESOLVE_SCRIPT_LIB` and
  `RESOLVE_EXECUTABLE` to the installed paths.
- DRX import fails: validate the six-node file and its SHA-256.
- LUT set fails: check `RESOLVE_LUT_DIR` or the platform LUT directory, then call
  `RefreshLUTList()`.
- Render settings reject `Main10`: remove unsupported keys; `ReplaceExistingFilesInPlace`
  is not supported by this Resolve build.
- Preview output missing: inspect `resolve_render_jobs.json` before rerunning.
- Final QC fails: keep the project and files, do not relabel the batch complete.
