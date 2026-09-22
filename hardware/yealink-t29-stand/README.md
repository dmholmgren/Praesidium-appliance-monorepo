# Yealink T29G desk stand (3D-printable)

Parametric replacement for the OEM T29G stand. It prints flat with the ribs facing up and needs no supports.

| File | What it is |
|---|---|
| `t29_stand.scad` | Source model. Every dimension is a parameter at the top of the file. |
| `t29_fit_test.stl` | A thin strip with both tabs at their real spacing. **Print this first** (~15 min). |
| `t29_stand.stl` | The full stand at the default parameters. |

![preview](preview.png)

## Measured vs. estimated

Each tab is two parallel rails standing out from the stand's edge face. The rails run along the edge and are flush with one face of the stand.

![tab detail](tab_detail.png)

| Parameter | Value | Source |
|---|---|---|
| `tab_w` | 18.0 mm | caliper, 18.00 mm (rail length along the edge) |
| `rail_z0` | 3.43 mm | 23.93 − 20.50, smooth face to the first rail |
| `rail_span` | 10.11 mm | caliper, outside of one rail to outside of the other |
| `rail_gap` | 4.24 mm | caliper, gap between the rails |
| `rail_h` | 3.90 mm | caliper; my reading is that this is how far the rails stand out |
| `edge_h` | 23.93 mm | caliper, stand thickness at the tab lip |
| tab A position | 32.93 mm | caliper, square step to the far end of tab A |
| tab B position | 5.81–25.67 mm | caliper, curved step to each end of tab B |
| tab spacing | 109.3 mm | worked out from the above; the tape measure agrees at about 110 |
| `foot_a_len` / `foot_b_len` | 38.44 / 44.99 mm | caliper, length of each foot along the edge |
| `notch_len` | 149 mm | caliper, gap between the feet |
| `bar_d` / `notch_d` | 44.99 / 15.63 mm | caliper; together they make 60.6 mm from the tab edge to the feet |
| `end_recess`, `edge_shift` | 8 / 16 mm | **estimated from photos**; these don't affect the fit |

Overall size: 232.4 × 60.6 mm, 23.9 mm tall at the tab lip. It fits a 235 mm or larger bed, or a 220 × 220 bed turned diagonally.

Cross-check across the edge face: 3.43 + 2.89 = 6.32 (measured 6.32); 2.89 + 4.24 + 2.89 = 10.11 (measured 10.11).

## Workflow

1. Print `t29_fit_test.stl` and push it into the phone's stand slots.
   * If the tabs are too tight or too loose, change `tab_clear` (default 0.15 mm per face).
   * If the tabs don't line up with the slots, change `tabA_from_step` / `tabB_from_step`.
   * If the fit test only goes in upside down, the rails are nearer the ribbed face than the smooth one. Set `rail_z0 = 10.39` (23.93 − 3.43 − 10.11), then mirror the full stand along X in the slicer.
2. Re-render:
   ```
   openscad -D 'part="stand"'    -o t29_stand.stl    t29_stand.scad
   openscad -D 'part="fit_test"' -o t29_fit_test.stl t29_stand.scad
   ```
3. Print the stands in PETG or PLA with 3 walls and 20% infill. Add stick-on rubber bumpers to the feet.
