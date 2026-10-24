import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { SEVERITY_COLOR } from "../format.js";

const STYLE = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors",
    },
  },
  layers: [{ id: "osm", type: "raster", source: "osm", paint: { "raster-saturation": -0.6 } }],
};

function toGeoJSON(hotspots) {
  return {
    type: "FeatureCollection",
    features: hotspots.map((h) => ({
      type: "Feature",
      id: h.id,
      geometry: { type: "Point", coordinates: [h.lon, h.lat] },
      properties: {
        id: h.id,
        color: h.state === "verified_fixed" ? "#30A46C" : SEVERITY_COLOR[h.severity],
        radius: h.severity === "high" ? 9 : h.severity === "medium" ? 7 : 5,
        ring: ["awaiting_verification", "reopened", "escalated"].includes(h.state) ? 3 : 0,
      },
    })),
  };
}

export default function MapView({ hotspots, selectedId, onSelect, onBounds }) {
  const el = useRef(null);
  const map = useRef(null);
  const fitted = useRef(false);

  useEffect(() => {
    const m = new maplibregl.Map({ container: el.current, style: STYLE, center: [77.209, 28.6139], zoom: 11 });
    map.current = m;
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    m.on("load", () => {
      m.addSource("hotspots", { type: "geojson", data: toGeoJSON([]), promoteId: "id" });
      m.addLayer({
        id: "hotspots",
        type: "circle",
        source: "hotspots",
        paint: {
          "circle-color": ["get", "color"],
          "circle-radius": ["get", "radius"],
          "circle-stroke-color": ["case", ["boolean", ["feature-state", "selected"], false], "#111", "#fff"],
          "circle-stroke-width": ["+", 1.5, ["get", "ring"]],
        },
      });
      m.on("click", "hotspots", (e) => onSelect(e.features[0].properties.id));
      m.on("mouseenter", "hotspots", () => (m.getCanvas().style.cursor = "pointer"));
      m.on("mouseleave", "hotspots", () => (m.getCanvas().style.cursor = ""));
    });
    m.on("moveend", () => {
      const b = m.getBounds();
      onBounds?.([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((x) => x.toFixed(5)).join(","));
    });
    return () => m.remove();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const m = map.current;
    if (!m) return;
    const apply = () => {
      const src = m.getSource("hotspots");
      if (!src) return;
      src.setData(toGeoJSON(hotspots));
      if (!fitted.current && hotspots.length) {
        const b = new maplibregl.LngLatBounds();
        hotspots.forEach((h) => b.extend([h.lon, h.lat]));
        m.fitBounds(b, { padding: 80, maxZoom: 16, duration: 0 });
        fitted.current = true;
      }
    };
    m.isStyleLoaded() && m.getSource("hotspots") ? apply() : m.once("idle", apply);
  }, [hotspots]);

  useEffect(() => {
    const m = map.current;
    if (!m || !m.getSource("hotspots")) return;
    hotspots.forEach((h) => m.setFeatureState({ source: "hotspots", id: h.id }, { selected: h.id === selectedId }));
  }, [selectedId, hotspots]);

  // Fly only when the selection changes, not on every refresh.
  useEffect(() => {
    const m = map.current;
    const h = hotspots.find((x) => x.id === selectedId);
    if (m && h) m.easeTo({ center: [h.lon, h.lat], zoom: Math.max(m.getZoom(), 15) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  return <div ref={el} className="map" />;
}
