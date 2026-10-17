# Third-party notices

## Bundled in this extension

These browser libraries are shipped in `media/vendor/`; their license texts are in `media/vendor/licenses/`.

| Package | License |
|---|---|
| marked | MIT |
| DOMPurify | MPL-2.0 OR Apache-2.0 (used under Apache-2.0) |
| highlight.js | BSD-3-Clause |

## Downloaded on first run (not bundled)

If no Python 3.9+ is installed, the extension downloads a standalone CPython 3.12 build from
[python-build-standalone](https://github.com/astral-sh/python-build-standalone) (BSD-3-Clause build
scripts; CPython itself is under the PSF-2.0 license, with the third-party licenses listed in the
distribution).

On first run the extension creates a Python virtual environment in its own storage folder and
installs these packages from PyPI with `pip`. They are not redistributed in the VSIX.

| Package | License |
|---|---|
| fastapi, pydantic, pydantic-core, annotated-types, annotated-doc, typing-inspection, anyio, h11, exceptiongroup, tiktoken, urllib3, charset-normalizer | MIT |
| starlette, uvicorn, click, httpx, httpcore, idna | BSD-3-Clause |
| requests, opentelemetry-api | Apache-2.0 |
| regex | Apache-2.0 AND CNRI-Python |
| typing_extensions | PSF-2.0 |
| certifi | MPL-2.0 (used unmodified) |
