# Xiaomi 15 Ultra Log Playbook

## Tested Source Profile

The source clips used to approve this skill were recorded by a Xiaomi 15 Ultra in Log mode.
Observed ffprobe properties:

- 3840 x 2160, HEVC Main 10, `yuv420p10le`
- Full range (`pc`)
- BT.2020 primaries and BT.2020 non-constant matrix
- Transfer unset in the container
- Variable timestamps around 59.97 fps
- AAC audio

This is not enough information to treat the file as HLG, PQ, S-Log3, or ARRI LogC3. The
transfer being unset is the important warning: a standard camera-log transform is not
available from metadata and should not be guessed.

## Why The Direct Route Exists

Two approved Xiaomi 15 Ultra jobs produced the original base looks:

| Preset | Direction |
| --- | --- |
| Documentary Clear | Expands the compressed black point, clears midtones, protects highlights, keeps neutral whites, controls green, and avoids forced warmth. |
| Clean Filmic | Soft filmic contrast, gentle highlight rolloff, restrained saturation, cool shadows, and slightly warm highlights. |

The batch Look Lab derives four additional restrained looks from those bases:

| Look ID | Direction |
| --- | --- |
| `natural_warm` | Warm correction for cool light and people |
| `cool_editorial` | Clean architectural and urban coolness |
| `market_rich` | More vivid but controlled market and food color |
| `soft_portrait` | Gentler highlights and restrained skin color |

Runtime LUTs are generated under `assets/luts/generated/`. Edit `assets/looks.json`, never the
generated `.cube` files. The runtime LUT is the same file used for both short previews and final
renders.

On a representative 4K frame, the direct ffmpeg result and approved Resolve render had nearly
identical mean RGB values; the remaining difference was mainly tiny spatial and encoding variation.
Do not insert an extra color-space conversion unless a new test shows that it improves the result.

## Direct Render Contract

- Apply one look LUT with `lut3d=...:interp=tetrahedral`.
- Output BT.709 / TV range metadata and 10-bit YUV.
- Keep the source frame size and orientation.
- Normalize smartphone VFR to 60000/1001 fps unless the user asks to preserve timestamps.
- Encode H.265 Main 10 with CRF 19 by default, `hvc1` tag, and fast-start.
- Re-encode audio to AAC 320 kbps, 48 kHz stereo.
- Write a QC manifest with source metadata, chosen LUT hash, output metadata, size, and SHA-256.

## Batch Look Lab

The batch route has four explicit stages:

1. `scan` probes every source, extracts three frames, computes objective metrics and writes a
   source contact sheet plus scouting template.
2. Codex inspects the source contact sheets and writes scene, light and risk tags.
3. `shortlist` recommends four looks, renders look grids and five-second H.264 previews.
4. The user picks looks; `render` outputs only those selected 4K H.265 combinations.

This is a selection-driven, two-phase route: preview and selection first, then final
render of only the chosen looks. Rendering every look on every 4K clip during preview
wastes time and disk without improving the decision.

## Preview Standard

Use three source moments: roughly 8%, 50%, and 86% through the clip. For short clips, use
5%, 50%, and 90%. Review the source and both looks side by side.

Good preview signs:

- White paper, screens, signs, and gray pavement do not carry an obvious color cast.
- Faces have believable hue and saturation.
- Signage and specular highlights keep detail.
- Shadow detail survives while blacks read as black, not milky gray.
- Green foliage and fluorescent light are controlled.

## When The Default Is Not Safe

Switch to a manual or Resolve-assisted review when:

- ffprobe reports 8-bit, SDR/Rec.709, a standard transfer tag, or a resolution below 4K.
- The image already looks display-ready rather than flat Log.
- The scene has a strong mixed-light cast and the fixed LUT cannot preserve neutral whites.
- Product colors are critical enough that a shot-specific white balance is needed.
- The user wants masks, selective color, tracking, or an editable timeline.

Do not fix a severe cast by stacking a generic log conversion before the LUT. Correct the
source balance first, then test the approved look.

## Route Selection

Use Resolve when the user asks for DaVinci Resolve, advanced grading, editability,
multiple look variants or maximum-quality 4K delivery. Use ffmpeg when Resolve is
unavailable, for quick analysis, or when the user explicitly requests the portable route.

For the Resolve route:

1. Import the Xiaomi clip and verify decoded orientation and full-range BT.2020 metadata.
2. Build the fixed six-node PowerGrade and use the approved per-shot CDL on node 2.
3. Inspect all source and look contact sheets before presenting the four candidates.
4. Render 1080p Resolve previews, then render only the user's selected looks.
5. QC the selected 4K HEVC Main 10 Rec.709 files and the batch manifest before delivery.

Resolve scripting requires a running Resolve instance with external scripting enabled.
If Resolve is unavailable, use the ffmpeg route for analysis or quick fallback and label
that result as ffmpeg output.

## Existing-Tool Findings

The local ecosystem was checked before making this skill:

- `scenario-video-color-grade` is a strong generic LUT/ffmpeg workflow, but it does not
  identify Xiaomi's untagged transfer or provide the approved Xiaomi looks.
- `davinci-resolve` can automate node graphs and renders, but requires the application and
  external scripting, so it is not the best default direct-output path.
- `gradekit` diagnoses SDR/8-bit footage and bakes corrective LUTs; its own documentation
  says HDR and 10-bit Log nuance are not modeled.
- The public `color-grade-skill` has a video workflow, but its Log path is specifically for
  Sony S-Log3.
- `davinci-resolve-mcp` and `mcp-video-studio` provide useful MCP layers, but neither has a
  Xiaomi-specific source profile or the user's approved look.

This skill fills that gap rather than duplicating a generic grading framework.
