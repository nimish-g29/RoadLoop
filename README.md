# RoadLoop

Pothole reports that actually get fixed: crowdsourced detection, plus TinyFish web agents that file, track and escalate.
Team SpiritX · HackIIITD final.

```
phone / dashcam clip ──► FastAPI ──► Celery ──► TinyFish (Agent · Search · Fetch · Monitor) ──► grievance portals
   detector (YOLO)        PostGIS      Redis            ▲                                              │
   IMU + GPS fusion       priority queue                └──── webhooks: run done / status changed ◄────┘
   blur all but pothole   lifecycle + audit ──► React + MapLibre engineer dashboard
```

## Quick start (offline, no TinyFish key needed)

```bash
cp .env.example .env            # TINYFISH_MODE=mock
docker compose up -d --build
docker compose run --rm api python -m pytest -q      # run the test suite first
python scripts/demo.py          # five-act scripted demo; open http://localhost:5173 alongside
```

The pieces:
- API docs: http://localhost:8000/docs
- Demo grievance portal: http://localhost:8100/portal/new
- Officer console (close tickets, fake a closure): http://localhost:8100/portal/admin

## Volunteer app (phone)

The volunteer app is a mobile web app served by the API at **`/app`**. There's nothing to install from a store.
Phones only allow camera and GPS over **https**, so open it through a tunnel:

```powershell
winget install --id Cloudflare.cloudflared      # once
cloudflared tunnel --url http://localhost:8000  # prints https://<random>.trycloudflare.com
```

1. Open `https://<random>.trycloudflare.com/app/` on the phone, then use "Add to Home Screen".
2. Set `PUBLIC_BASE_URL` in `.env` to the same https URL. Report photos and TinyFish webhooks use it. Then restart with `docker compose up -d`.

The app has three tabs:
- **Drive:** mount the phone, and the camera plus on-device YOLO run 3–5 times per second. The app uploads one redacted frame + GPS + jolt per pothole and nothing else. It also re-checks "portal says fixed" spots automatically. "Mark pothole" lets a rider or passenger tag one by hand. Demo options: play a recorded dashcam video instead of the camera, and simulated GPS.
- **Report:** take a photo, tap the pothole, pick a size, send. GPS is automatic, and everything except the tapped area is blurred on the phone.
- **My reports:** live status and complaint IDs. **Approve filing** is the consent step before a TinyFish agent files.

Reports made offline wait on the phone and send when the connection returns.
**Automatic detection needs a model:** put `pothole.onnx` (320 px export from `detector/train.py`) and `model.json` in `backend/app/static/rider/model/`. Until then, drive mode works with "Mark pothole" only.
New hotspots are reverse geocoded (road name, address, city, state) through OpenStreetMap Nominatim. Set `GEOCODER=off` to disable this.

## Going live with TinyFish

1. Get a key at agent.tinyfish.ai/api-keys. Set `TINYFISH_MODE=live` and `TINYFISH_API_KEY` in `.env`.
2. TinyFish must reach your webhooks over **https**. Run `cloudflared tunnel --url http://localhost:8000`, or ngrok, and set `PUBLIC_BASE_URL` to the tunnel URL. Without https, RoadLoop falls back to polling and skips Monitors.
3. For a live, CAPTCHA-free demo, also tunnel the mock portal (port 8100). Replace `{MOCK}` for `demo` in `backend/app/tinyfish/portals.json` with that public URL. The **real** TinyFish agent then fills a real web form you control.
4. For real portals, walk each one with TinyFish first. Then fill its entry in `portals.json` (filing URL, status mode, OTP) and set `"verified": true`.

## What's where

| Path | What |
|---|---|
| `backend/app/core/` | Pure logic: geohash dedupe, severity / confidence / priority, lifecycle state machine, portal-status parser (English and Hindi), verification rule, authority routing |
| `backend/app/tinyfish/` | TinyFish client (matches docs.tinyfish.ai, Oct 2026), agent goals and output schemas, portal registry |
| `backend/app/services/workflow.py` | The loop: ingest → merge → confirm → approve → file → track → verify → escalate |
| `backend/app/worker.py` | Celery tasks and beat (status checks every 30 min, SLA every hour, priorities every 15 min) |
| `backend/app/main.py` | REST API for the rider app and dashboard, plus webhooks |
| `mock_tinyfish/` | Same REST shapes as TinyFish, plus the fictional demo portal |
| `detector/` | YOLO ONNX inference, IMU/GPS fusion, per-pass debouncing, privacy redaction, dashcam CLI, training/export |
| `backend/app/static/rider/` | Volunteer mobile web app: drive mode (camera + onnxruntime-web YOLO + motion + GPS), photo reports, my reports/approval, offline queue |
| `dashboard/` | React + MapLibre engineer queue: ranked list, map, SLA clocks, complaint IDs, audit timeline |
| `scripts/demo.py` | The judge demo, scripted |

## Design decisions worth saying out loud

- **Severity classes, not millimetres.** Area + speed-normalised jolt + road type. A hotspot's severity is the 70th percentile of its detections, so one bad frame can't escalate it.
- **Distinct riders, not passes.** Confidence is a noisy-OR over riders. Filing needs ≥2 riders and ≥0.85 confidence.
- **Consent is enforced in code.** Only a rider who reported the pothole can approve filing, under their own name. Agents stop on OTP/CAPTCHA and return `needs_human`.
- **TinyFish usage is quota-aware.**
  - One Page Monitor per open complaint, deleted on closure (there's a per-account cap).
  - If a Monitor can't be created, it falls back to batched Fetch (10 URLs per call).
  - Login-only trackers get scheduled Agent lookups instead.
- **Webhooks plus polling.** Both paths are idempotent, because TinyFish webhook delivery is best effort.
- **Physical evidence beats a "Closed" label.** 2 clear passes means verified fixed. 2 sightings, or 1 strong detection with a jolt, reopens the ticket and escalates with the original complaint ID.

## Status

| Part | State |
|---|---|
| Core logic, TinyFish client vs mock, detector pipeline | Tested here (32 tests) |
| DB/API flow (`tests/test_flow.py`), Celery, PostGIS SQL, dashboard | Written and syntax-checked, **not yet run**: the build sandbox had no PyPI/npm access. Run the test suite first. |
| Pothole weights | Not included. Run `detector/train.py` on a pothole dataset plus your own Delhi/Patna frames. |
| Volunteer web app | Node unit tests (8) and a headless-Chromium end-to-end test (fake camera + GPS) pass against a stub API. Not yet run against the real stack or on a real phone. |

## Day-one checklist

- [ ] `docker compose run --rm api python -m pytest -q` passes
- [ ] `python scripts/demo.py --no-pause` completes in mock mode
- [ ] TinyFish key works: a `run` on the tunnelled demo portal returns a complaint ID
- [ ] Find out whether the agent can **upload the photo** (the docs don't say). If not, the photo link stays in the description.
- [ ] Walk one real portal (CPGRAMS or a city portal) and note where OTP/login blocks
- [ ] Train the detector; record 2-3 dashcam clips with GPS and IMU logs (e.g. Physics Toolbox / GPSLogger apps)
