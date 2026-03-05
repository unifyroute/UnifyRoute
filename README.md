# UnifyRoute 🚦

![Status](https://img.shields.io/badge/Status-Active_Development-2ea44f)
![API](https://img.shields.io/badge/API-OpenAI_Compatible-0ea5e9)
![License](https://img.shields.io/badge/License-Apache_2.0-f59e0b)

UnifyRoute is a self-hosted, OpenAI-compatible gateway for routing requests across multiple LLM providers with failover, quota awareness, and a management UI.

## Why UnifyRoute ✨

- OpenAI-compatible API endpoints for easy SDK/tool integration.
- Tier-based routing with fallback behavior and provider redundancy.
- Credential and provider management through an interactive dashboard.
- Cost, usage, and operational visibility for production workflows.

## Key Labels 🏷️

- `SELF-HOSTED`
- `OPENAI-COMPATIBLE`
- `MULTI-PROVIDER-ROUTING`
- `FAILOVER-READY`
- `OPS-FRIENDLY`

## Quick Start 🚀

```bash
git clone https://github.com/unifyroute/UnifyRoute.git
cd UnifyRoute

cp sample.env .env
./unifyroute setup
./unifyroute start
```

Dashboard: `http://localhost:6565`

Windows note (Git Bash): if `./unifyroute ...` fails due to interpreter resolution, confirm `python --version` or `py --version` works.

## Documentation 📚

- [Getting Started](docs/getting-started.md)
- [CLI Reference](docs/cli.md)
- [Configuration Reference](docs/configuration.md)
- [Architecture Overview](docs/architecture.md)
- [Development Guide](docs/development.md)
- [Migration Guide](MIGRATION_GUIDE.md)

## Open Source Project Files 🤝

- [Contributing Guide](CONTRIBUTING.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Security Policy](SECURITY.md)
- [Support](SUPPORT.md)
- [Changelog](CHANGELOG.md)
- [License (Apache 2.0)](LICENSE)

## Repository Layout 🧭

```text
api-gateway/      FastAPI gateway API and routes
router/           Routing engine and provider adapters
shared/           Shared models, schemas, and security helpers
gui/              React dashboard
credential-vault/ OAuth/secret helper service
quota-poller/     Quota and model sync workers
launcher/         Unified app launcher
docs/             Documentation
scripts/          Utility and setup scripts
```

## API Compatibility 🔌

UnifyRoute exposes OpenAI-style endpoints:

- `POST /api/v1/chat/completions`
- `POST /api/v1/completions`
- `GET /api/v1/models`

## License ⚖️

Apache License 2.0. See `LICENSE`.
