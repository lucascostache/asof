# Licences: AsOf and what it installs

AsOf (`strathora-asof`) is licensed under **Apache-2.0** (`LICENSE`, `NOTICE`).

**The core has no dependencies.** `pip install` of the package alone installs one distribution, `strathora-asof`, and uses only the Python standard library (checked on every run of `scripts/clean_install_check.sh`, step 1). No third-party library is copied into the repository: the widget script and stylesheet in `asof/widgets/` were written for this project.

Everything below is **optional** and is installed only if you ask for an extra. The licences are what each package declares in its own metadata, read on 2026-11-12 from a new environment (Python 3.13, macOS) after `pip install "strathora-asof[mcp,parquet,server]"`. This is a list, not a legal review: versions are ranges, so a later install can bring other versions and other packages, and each package's own licence file is the authority.

## The extras you can ask for

| Extra | Installs directly | Licence | What it is for |
|---|---|---|---|
| `mcp` | `mcp` (2.x) | MIT | the MCP server, `strathora serve-mcp` |
| `parquet` | `pyarrow` (14 or newer) | Apache-2.0 | reading Parquet files as a source |
| `server` | `certifi` | MPL-2.0 | trusted TLS roots for sync and SEC fetches on a Python that ships none |
| `dev` | `pytest` (MIT), `hypothesis` (MPL-2.0), `ruff` (MIT), `mypy` (MIT), and the three above | | running the tests; not needed to use AsOf |

## What the `mcp` extra brings with it

`pyarrow` and `certifi` brought nothing else. `mcp` brought 27 more packages:

| Package | Version seen | Licence declared |
|---|---|---|
| annotated-types | 0.8.0 | MIT |
| anyio | 4.16.0 | MIT |
| attrs | 26.1.0 | MIT |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.1 | BSD-3-Clause |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| httpcore2 | 2.16.0 | BSD-3-Clause |
| httpx2 | 2.16.0 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| mcp-types | 2.3.0 | MIT |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| pycparser | 3.12 | BSD-3-Clause |
| pydantic | 2.14.3 | MIT |
| pydantic_core | 2.50.4 | MIT |
| pyjwt | 2.15.1 | MIT |
| python-multipart | 0.0.33 | Apache-2.0 |
| referencing | 0.37.0 | MIT |
| rpds-py | 2026.9.1 | MIT |
| sse-starlette | 3.7.0 | BSD-3-Clause |
| starlette | 1.10.0 | BSD-3-Clause |
| truststore | 0.10.5 | MIT |
| typing-inspection | 0.4.5 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| uvicorn | 0.54.0 | BSD-3-Clause |

`mcp` itself was 2.3.0, `pyarrow` 26.0.1 and `certifi` 2026.10.23.

## What to notice

* Everything is under a permissive licence (MIT, MIT-0, BSD-3-Clause, Apache-2.0, PSF-2.0) except **`certifi` and `hypothesis`, which are MPL-2.0**: a file-level copyleft licence. AsOf uses both unchanged and copies neither, and `hypothesis` is a test tool only.
* AsOf does not redistribute any of these packages: `pip` downloads them from the package index when you install an extra.
* Not checked: licences of the packages on other operating systems or Python versions (some packages are installed only on some of them), and the licence texts themselves.

To see what your own environment holds: `pip list`, then `pip show NAME` for the licence of each.
