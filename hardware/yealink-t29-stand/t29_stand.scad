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
// face, running along the edge, flush with one face of the stand.
tab_w        = 19.0;   // rail length along the stand edge (measured 18.94)
rail_span    = 10.11;  // across both rails, outside to outside (measured)
rail_gap     = 4.24;   // gap between the rails (measured)
rail_h       = 3.90;   // how far the rails stand out from the edge face (measured -- CONFIRM)
edge_h       = 20.50;  // stand thickness at the tab, rail face to far face (measured)
tab_pitch    = 120.0;  // tab centre-to-centre distance                   (ESTIMATE - measure)
tab_offset   = 0.0;    // shift both tabs left(-)/right(+) if the slots are off-centre
tab_clear    = 0.15;   // removed from each tab face so printed tabs slide in
tab_chamfer  = 0.6;    // lead-in chamfer on the rail tips

/* [Stand body] */
stand_w      = 180.0;  // overall width, foot to foot
stand_len    = 75.0;   // tab edge to foot edge -- sets the viewing angle (longer = more upright)
band_d       = 32.0;   // depth of the solid band along the tab edge
leg_w        = 28.0;   // width of each leg
corner_r     = 6.0;    // outline corner radius
skin         = 2.4;    // flat plate thickness
rib_h        = 10.0;   // total height incl. stiffening ribs (bosses at the tabs go to edge_h)
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
    // Rails flush with the bed face (z = 0) so the lower rail prints on the bed.
    pitch = (rail_span + rail_gap)/2;
    for (z = [tab_clear, tab_clear + pitch])
        translate([0, 0, z]) rail();
    // Boss behind the rails, full stand thickness at the tab.
    translate([-tab_w/2 - 3, 0, 0]) cube([tab_w + 6, 12, edge_h]);
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
}

module stand() {
    linear_extrude(skin) outline();
    ribs();
    tabs();
}

module fit_test() {
    span = tab_pitch + tab_w + 12;
    translate([tab_offset - span/2, 0, 0]) cube([span, fit_strip_d, fit_strip_t]);
    tabs();
}

if (part == "fit_test") fit_test();
else stand();
