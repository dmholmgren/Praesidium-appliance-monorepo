# Yealink T29G desk stand (3D-printable)

Parametric replacement for the OEM T29G stand. It prints flat with the ribs facing up and needs no supports.

| File | What it is |
|---|---|
| `t29_stand.scad` | Source model. Every dimension is a parameter at the top of the file. |
| `t29_fit_test.stl` | A thin strip with both tabs at their real spacing. **Print this first** (~15 min). |
| `t29_stand.stl` | The full stand at the default parameters. |

![preview](preview.png)

## Measured vs. estimated

| Parameter | Value | Source |
|---|---|---|
| `tab_w` | 19.0 mm | caliper, 18.94 mm (25.67 − 5.81 ≈ 19.9 agrees) |
| `tab_t` | 6.3 mm | caliper, 6.32 mm |
| `tab_pitch` | 120 mm | **estimate from photo**, tab centre to centre |
| `tab_len` | 8 mm | **estimate**, how far a tab sticks out past the edge |
| `stand_len` | 75 mm | **design choice**, sets the viewing angle |
| `stand_w` | 180 mm | design choice, foot to foot |

Before a batch run, confirm `tab_pitch` and `tab_len` against an OEM stand. The fit test checks both.

## Workflow

1. Print `t29_fit_test.stl` and push it into the phone's stand slots.
   * If the tabs are too tight or too loose, change `tab_clear` (default 0.15 mm per face).
   * If the tabs don't line up with the slots, change `tab_pitch` / `tab_offset`.
   * If the stand needs to click in and stay put, set `snap_bump` to about 0.4.
2. Re-render:
   ```
   openscad -D 'part="stand"'    -o t29_stand.stl    t29_stand.scad
   openscad -D 'part="fit_test"' -o t29_fit_test.stl t29_stand.scad
   ```
3. Print the stands in PETG or PLA with 3 walls and 20% infill. Add stick-on rubber bumpers to the feet.
