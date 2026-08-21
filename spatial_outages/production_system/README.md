# IEX outage attribution service

The production path uses three real sources:

1. `GET /get_outage_attribution?status=OPEN` for frozen outage IDs, members, and duration.
2. A fresh Customer V2 CSV for CSP ownership and coordinates.
3. Remote's batch device-ping API for last successful ping time.

Run:

```bash
python refresh_customer_v2.py
python app.py --port 8000
```

Defaults:

- Customer V2: `../data/input/outage_devices.csv`
- Outage feed: `https://router-outage-detection.i2e1.in/get_outage_attribution?status=OPEN`
- Device ping: `https://remote.i2e1.in/REMOTE/GetBatchDevicePing`
- Refresh: every 60 seconds
- Maximum Customer V2 snapshot age: 24 hours

Startup fails when Customer V2 is stale, the outage feed is malformed/unavailable, or the ping API cannot serve the required device states. There is no production fallback to synthetic data or historical ping columns.

For a real-feed localhost preview without device ping, use `python app.py --status-url '' --allow-missing-status`. All unavailable device states and causes remain `UNKNOWN`; no UP/DOWN state is invented.

The service keeps `POST /outage_attribution` for TFF calls:

```bash
curl -s http://127.0.0.1:8000/outage_attribution \
  -H 'Content-Type: application/json' \
  -d '{"outage_id":2939,"devices":["GX100305"],"ongoing_time":12900}'
```

Response:

```json
{"outage_id":2939,"attribution":"UNKNOWN","confidence":"LOW"}
```

`GetBatchDevicePing` is called with POST JSON batches of at most 200 device IDs. Its UTC `latestPing` value drives state: less than 10 minutes is UP, at least 10 minutes is DOWN, and missing/invalid/future is UNKNOWN. API failure or truncation fails the refresh closed.
