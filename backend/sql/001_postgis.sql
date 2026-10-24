-- Spatial column + index on hotspots. The ORM stores lat/lon; PostGIS keeps a
-- generated geography point for radius queries (verification tasks, map bbox).
ALTER TABLE hotspots ADD COLUMN IF NOT EXISTS geom geography(Point, 4326)
  GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography) STORED;
CREATE INDEX IF NOT EXISTS ix_hotspots_geom ON hotspots USING GIST (geom);
CREATE INDEX IF NOT EXISTS ix_hotspots_open_priority ON hotspots (priority DESC)
  WHERE state NOT IN ('verified_fixed');
