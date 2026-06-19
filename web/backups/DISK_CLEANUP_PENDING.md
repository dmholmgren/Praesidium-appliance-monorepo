# Marcus disk reclaim — PENDING (future pass)

Matter: d389d889-4fe9-41c9-b74e-622d93d244f8 (Marcus 9510.007)
DB teardown completed 2026-06-14: all collections / docs / stage rows deleted; 1 manifest row retained (append-only, orphaned).

## PRESERVE (do not delete)
/mnt/ediscovery/986c0fee-1390-43bb-ad28-8cd1db6de53f/d389d889-4fe9-41c9-b74e-622d93d244f8/Marcus_production/originals/
  - as_received/  ~251G  (the 250GB Relativity zip: 20250725 Production 01.001-003)
  - unpacked/20250725 Production 01/  ~286G  (extraction; re-ingest points here)

## RECLAIM (~2TB; everything OUTSIDE the preserve path)
- Marcus_production/{native,text,working,renditions}   (~292G, regenerable)
- Marcus_production-_07.25.25/                          (746G, duplicate production)
- the 222 Client Documents collection dirs             (remainder of the 2.5TB)

Matter root total at teardown: 2.5T across 224 dirs. Keep ~537G, reclaim ~2TB.
Every rm target is outside Marcus_production/originals/.
