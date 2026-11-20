# Look Lab Review Guide

## Scouting Vocabulary

Use only these tags in `scouting.json`.

Scene:

- `people`: groups or people in the frame.
- `portrait`: a person or small group is the primary subject.
- `market`: stalls, retail, boxes, cargo, street commerce.
- `food`: food is visually prominent.
- `products`: merchandise or packaging carries the frame.
- `indoor`: interior architecture or institutional space.
- `architecture`: built geometry is a major subject.
- `urban`: streets, traffic, city structures.
- `nature`: vegetation, landscape or outdoor growth.
- `document`: signs, screens or information-heavy surfaces.

Light:

- `daylight`: clean daylight.
- `overcast`: flat gray daylight.
- `mixed`: competing daylight and artificial sources.
- `fluorescent`: green-biased interior lighting.
- `night`: low ambient light or night scenes.
- `backlight`: strong light behind the subject.

Risks:

- `skin`: skin color is critical.
- `signage_highlight`: illuminated signs or screens can clip.
- `neutral_white`: white paper, screens, walls or text must remain neutral.
- `dominant_color`: the source already has a strong cast or dominant hue.
- `clipping`: highlights or shadows already clip heavily.

## Scouting Procedure

Open `source_contact.jpg` at a readable size. Record what is actually visible:

1. What carries the frame: people, products, architecture, food, nature or text?
2. What is the dominant light source and its color temperature?
3. Which parts must not shift: skin, white text, neutral walls, product colors?
4. Is the source already dark, bright, heavily saturated or clipped?

Do not infer scene tags from filenames or timestamps. Do not add tags that are not visible.

## Shortlist Review

Open each clip's `look_contact.jpg`.

Accept a shortlist when:

- The four looks differ in a way a normal viewer can see.
- `Documentary Clear` remains a useful neutral baseline.
- White text and neutral surfaces stay neutral.
- Bright signs retain readable detail.
- Skin remains believable; it must not become orange, pink or gray.
- Products and food gain energy without neon saturation.
- Cool and warm looks remain restrained enough for documentary footage.

If two candidates are nearly identical, inspect their generated LUTs and operations. Adjust
`assets/looks.json`, regenerate LUTs, and rerun `shortlist`.

## Presentation Format

Present one compact table per batch:

| Clip | Source finding | A | B | C | D |
| --- | --- | --- | --- | --- | --- |
| clip_001 / filename | market, overcast, skin | Documentary Clear | Market / Food Rich | Natural Warm | Soft Portrait |

Include the preview paths below the table. Keep the real look IDs visible even when using A/B/C/D.

## Mapping A User Choice

Write only the selected look IDs:

```json
{
  "clip_001": {
    "filename": "VID_example.mp4",
    "looks": ["market_rich"]
  },
  "clip_002": {
    "filename": "VID_example_2.mp4",
    "looks": ["cool_editorial", "documentary"]
  }
}
```

- Every shortlisted clip must have at least one look.
- Duplicate looks for the same clip are invalid.
- A look can be selected for one clip without affecting other clips.
- Multiple looks may be selected for one clip when the user explicitly asks for both.

Echo the parsed selection before starting a long final render.

## Failure Handling

- `source_matches_tested_profile: false`: previews may be inspected, but final rendering is blocked.
- `status: partial` after `scan`: report failed clips before scouting.
- `status: partial` after `shortlist`: the user can still choose among successfully shortlisted clips,
  but the missing clips must be reported.
- `status: partial` after `render`: report exactly which clip/look pairs rendered and which failed.
- A final QC failure is not a warning. Do not present that output as approved.

## Output Layout

```text
Xiaomi15U_LookLab_YYYYMMDD_HHMMSS/
  batch_manifest.json
  scouting_template.json
  scouting.json
  selection_template.json
  selections.json
  final_manifest.json
  qc_report.json
  clips/
    clip_001/
      analysis/
        probe.json
        metrics.json
        source_contact.jpg
        source_frames/
      previews/
        <look-id>/
          preview.mp4
          01.png
          02.png
          03.png
      look_contact.jpg
      preview_manifest.json
  final/
    clip_001__<look-id>.mp4
```
