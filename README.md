# Fuel Route API

Django API for finding a driving route and cost-effective fuel stops along it.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Set API keys:

```bash
export MAPTILER_API_KEY='your-maptiler-key'
export GEOAPIFY_API_KEY='your-geoapify-key'
```

The app reads geocoded stations from:

```text
scripts/fuel-prices-geocoded.csv
```

Override with `FUEL_PRICES_CSV=/path/to/file.csv` if needed.

## Run

```bash
.venv/bin/python manage.py runserver
```

## API

```bash
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start":"New York, NY","finish":"Chicago, IL"}'
```

The response includes:

- geocoded start and finish
- route distance, duration, and GeoJSON
- selected fuel stops
- total fuel spend assuming 10 MPG and a full tank at departure
- MapTiler style URL and fuel-stop marker data for map rendering
