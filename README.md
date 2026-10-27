# MM3

MM3 is a modular smart-mirror platform written in Python, inspired by
[MagicMirror²](https://magicmirror.builders). It keeps MagicMirror²'s ideas —
modules, notifications, a kiosk display — with a typed Python engine, a
configurable screen grid, a web admin UI and a signed module catalog.

> **Status:** pre-alpha. The specifications are complete; the engine is being
> implemented milestone by milestone. Nothing is usable yet.

MM3 is an independent project and is not affiliated with the MagicMirror² project.

## Highlights

- **Python 3.11+ asyncio engine** on FastAPI/Uvicorn, for Debian 12+ on x86-64.
- **Modules** are pip packages: an optional Python backend plus an optional
  Web Component front-end, behind one documented contract.
- **Configurable grid layout** with a live preview editor in the admin UI.
- **Configuration** as YAML or through the admin UI (layout, forms, YAML),
  with the same validator for both.
- **MQTT and Home Assistant** integration, including MQTT discovery.
- **No-code widget builder** for MQTT, Home Assistant, bus and HTTP data.
- **Five languages** out of the box: English, Italian, French, Spanish, German.
- **Migration tool** for MagicMirror² configs and modules.

## Specifications

The specifications are authoritative; see [`docs/spec/`](docs/spec/README.md).

- **Core Engine Specification** — engine behaviour (`CORE-xx`, `WGT-xx`)
- **Module Contract v1.0** — what every module must do (`MOD-xx`)

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,migrate]"
pre-commit install
pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions.

## Related repositories

- [`module-index`](https://github.com/mm3-project/module-index) — the public module catalog

## License

[MIT](LICENSE)
