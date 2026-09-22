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
tab_w        = 19.0;   // tab width along the stand edge (measured 18.94; 25.67 - 5.81 = 19.86)
tab_t        = 6.3;    // tab thickness (measured 6.32)
tab_len      = 8.0;    // how far the tab sticks out past the stand edge  (ESTIMATE - measure)
tab_pitch    = 120.0;  // tab centre-to-centre distance                   (ESTIMATE - measure)
tab_offset   = 0.0;    // shift both tabs left(-)/right(+) if the slots are off-centre
tab_clear    = 0.15;   // removed from each tab face so printed tabs slide in
tab_chamfer  = 1.0;    // lead-in chamfer on the tab tip
snap_bump    = 0.0;    // height of a retention ridge on the tab top face (0 = none, try 0.4)

/* [Stand body] */
stand_w      = 180.0;  // overall width, foot to foot
stand_len    = 75.0;   // tab edge to foot edge -- sets the viewing angle (longer = more upright)
band_d       = 32.0;   // depth of the solid band along the tab edge
leg_w        = 28.0;   // width of each leg
corner_r     = 6.0;    // outline corner radius
skin         = 2.4;    // flat plate thickness
rib_h        = tab_t;  // total height incl. stiffening ribs
rib_t        = 2.0;    // rib / perimeter wall thickness
rib_pitch    = 20.0;   // grid rib spacing

/* [Fit test] */
fit_strip_d  = 14.0;   // depth of the test strip
fit_strip_t  = 2.4;    // thickness of the test strip between the tabs

$fn = 48;

// ---------------------------------------------------------------------------
// Geometry: X = along the tab edge, Y = away from the tabs (towards the feet),
// Z = up off the print bed. Tabs protrude in -Y from the edge at y = 0.

module rounded_square(size, r) {
    offset(r) offset(-r) square(size);
}

module outline() {
    // Rectangle with the centre cut away below the band, leaving two legs.
    difference() {
        translate([-stand_w/2, 0]) rounded_square([stand_w, stand_len], corner_r);
        translate([-stand_w/2 + leg_w, band_d])
            offset(corner_r) offset(-corner_r)
                square([stand_w - 2*leg_w, stand_len]);
    }
}

module tab_trimmed() {
    w = tab_w - 2*tab_clear;
    t = tab_t - 2*tab_clear;
    c = tab_chamfer;
    hull() {
        // root (inside the body)
        translate([-w/2, -tab_len + c, tab_clear]) cube([w, tab_len - c + 1, t]);
        // tip, shrunk by the chamfer
        translate([-w/2 + c, -tab_len, tab_clear + c]) cube([w - 2*c, 0.01, t - 2*c]);
    }
    if (snap_bump > 0)
        translate([-w/2 + c, -tab_len + c + 1.5, tab_clear + t - 0.01])
            rotate([0, 90, 0]) cylinder(r = snap_bump, h = w - 2*c, $fn = 16);
}

module tabs() {
    for (s = [-1, 1])
        translate([tab_offset + s*tab_pitch/2, 0, 0]) tab_trimmed();
}

module ribs() {
    // Perimeter wall
    linear_extrude(rib_h)
        difference() { outline(); offset(-rib_t) outline(); }
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
    // Solid bosses behind each tab so the tab load goes into the plate
    for (s = [-1, 1])
        translate([tab_offset + s*tab_pitch/2 - tab_w/2 - rib_t, 0, 0])
            cube([tab_w + 2*rib_t, 10, rib_h]);
}

module stand() {
    linear_extrude(skin) outline();
    ribs();
    tabs();
}

module fit_test() {
    span = tab_pitch + tab_w + 12;
    translate([tab_offset - span/2, 0, 0]) cube([span, fit_strip_d, fit_strip_t]);
    for (s = [-1, 1])
        translate([tab_offset + s*tab_pitch/2 - tab_w/2 - 3, 0, 0])
            cube([tab_w + 6, fit_strip_d, tab_t]);
    tabs();
}

if (part == "fit_test") fit_test();
else stand();
