// Yealink T29G replacement desk stand -- parametric, prints flat, no supports.
//
// Render one part at a time:
//   openscad -D 'part="stand"'    -o t29_stand.stl    t29_stand.scad
//   openscad -D 'part="fit_test"' -o t29_fit_test.stl t29_stand.scad
//
// PRINT THE FIT TEST FIRST. It is a thin strip carrying both tabs at the
// real spacing, so one ~15 minute print confirms tab size, tab spacing and
// slot engagement before committing filament to a batch of full stands.

part = "stand";            // "stand" | "fit_test"

/* [Tabs -- from caliper measurements of the OEM stand] */
// Each tab is a pair of parallel rails standing out from the stand's edge
// face, running along the edge. Across the edge face (bed face upward):
//   3.43 face->rail1 | 2.89 rail1 | 4.24 gap | 2.89 rail2 | 10.39 -> far face
// Checks: 6.32 = face->rail1 inner, 10.11 = rail span, 20.50 = rail1 outer->far face,
//         23.93 = full edge thickness.
tab_w        = 18.0;   // rail length along the stand edge (measured 18.00)
rail_z0      = 3.43;   // bed face to the outer side of the first rail (23.93 - 20.50)
rail_span    = 10.11;  // across both rails, outside to outside (measured)
rail_gap     = 4.24;   // gap between the rails (measured)
rail_h       = 3.90;   // how far the rails stand out from the edge face (measured -- CONFIRM)
edge_h       = 23.93;  // stand thickness at the tab (measured)
// Tab positions along the edge come from the OEM outline (see "Stand body"):
//   tab A: 32.93 from the square step to its far end   -> centre 23.93 in from the step
//   tab B: 5.81 .. 25.67 from the curved step           -> centre 15.74 in from the step
// giving ~109 mm centre to centre (tape measure agrees at ~110).
tabA_from_step = 32.93 - 18.0/2;
tabB_from_step = (5.81 + 25.67)/2;
tab_clear    = 0.15;   // removed from each tab face so printed tabs slide in
tab_chamfer  = 0.6;    // lead-in chamfer on the rail tips

/* [Stand body -- OEM outline] */
// Plan view, smooth face down, tabs pointing -Y. Square-step end at -X,
// curved-step end at +X. Measured values unless noted.
foot_a_len   = 38.44;  // foot at the square-step end, along the edge
foot_b_len   = 45.00;  // foot at the curved-step end (44.99)
notch_len    = 149.0;  // gap between the feet along the edge
bar_d        = 45.0;   // tab edge to the notch between the feet (44.99)
notch_d      = 15.63;  // how far the feet reach past the bar
end_recess   = 8.0;    // how far the ends sit back from the tab edge   (ESTIMATE from photo)
edge_shift   = 16.0;   // tab edge starts this far in from the foot-A end...(ESTIMATE; see below)
corner_r     = 3.0;    // outline corner radius
skin         = 2.4;    // flat plate thickness
rib_h        = 10.0;   // total height incl. stiffening ribs
lip_t        = 3.0;    // full-height lip along the tab edge (edge_h tall)
rib_t        = 2.0;    // rib / perimeter wall thickness
rib_pitch    = 20.0;   // grid rib spacing

stand_w      = foot_a_len + notch_len + foot_b_len;   // 232.4 overall
stand_len    = bar_d + notch_d;                        // 60.6 tab edge to feet
// The straight tab edge is the same length as the notch but shifted toward
// foot A: it runs from (foot_a_len - edge_shift) to (foot_a_len - edge_shift + notch_len).
edge_u0      = foot_a_len - edge_shift;
edge_u1      = edge_u0 + notch_len;
tabA_x       = -stand_w/2 + edge_u0 + tabA_from_step;
tabB_x       = -stand_w/2 + edge_u1 - tabB_from_step;
tab_pitch    = tabB_x - tabA_x;
echo(stand_w = stand_w, stand_len = stand_len, tab_pitch = tab_pitch);

/* [Fit test] */
fit_strip_d  = 14.0;   // depth of the test strip
fit_strip_t  = 2.4;    // thickness of the test strip between the tabs

$fn = 48;

// ---------------------------------------------------------------------------
// Geometry: X = along the tab edge, Y = away from the tabs (towards the feet),
// Z = up off the print bed. Tabs protrude in -Y from the edge at y = 0.

module raw_outline() {
    L = stand_w; D = stand_len;
    translate([-L/2, 0]) polygon([
        [0, end_recess], [edge_u0, end_recess], [edge_u0, 0], [edge_u1, 0],
        [edge_u1, end_recess], [L, end_recess], [L, D],
        [L - foot_b_len, D], [L - foot_b_len, bar_d],
        [foot_a_len, bar_d], [foot_a_len, D], [0, D]]);
}

module outline() {
    // Round both convex and concave corners.
    offset(corner_r) offset(-2*corner_r) offset(corner_r) raw_outline();
}

module rail() {
    // One rail: length tab_w along X, width rw in Z, standing rail_h out in -Y.
    rw = (rail_span - rail_gap)/2 - 2*tab_clear;
    h  = rail_h - tab_clear;
    c  = tab_chamfer;
    hull() {
        translate([-tab_w/2 + c, -h, 0]) cube([tab_w - 2*c, h + 1, rw]);
        translate([-tab_w/2, -h + c, 0]) cube([tab_w, h - c + 1, rw]);
    }
}

module tab_trimmed() {
    pitch = (rail_span + rail_gap)/2;
    for (z = [rail_z0 + tab_clear, rail_z0 + tab_clear + pitch])
        translate([0, 0, z]) rail();
    // Boss behind the rails, full stand thickness at the tab.
    translate([-tab_w/2 - 3, 0, 0]) cube([tab_w + 6, 12, edge_h]);
}

module tabs() {
    for (s = [-1, 1])
        translate([s < 0 ? tabA_x : tabB_x, 0, 0]) tab_trimmed();
}

module ribs() {
    // Perimeter wall
    linear_extrude(rib_h)
        difference() { outline(); offset(-rib_t) outline(); }
    // Full-height lip along the tab edge, as on the OEM part
    translate([-stand_w/2 + edge_u0 + corner_r, 0, 0])
        cube([notch_len - 2*corner_r, lip_t, edge_h]);
    // Grid, clipped to the outline
    intersection() {
        linear_extrude(rib_h) outline();
        union() {
            for (x = [-stand_w/2 : rib_pitch : stand_w/2])
                translate([x - rib_t/2, 0, 0]) cube([rib_t, stand_len, rib_h]);
            for (y = [rib_pitch : rib_pitch : stand_len])
                translate([-stand_w/2, y - rib_t/2, 0]) cube([stand_w, rib_t, rib_h]);
        }
    }
}

module stand() {
    linear_extrude(skin) outline();
    ribs();
    tabs();
}

module fit_test() {
    x0 = tabA_x - tab_w/2 - 6;
    translate([x0, 0, 0]) cube([tab_pitch + tab_w + 12, fit_strip_d, fit_strip_t]);
    tabs();
}

if (part == "fit_test") fit_test();
else stand();
