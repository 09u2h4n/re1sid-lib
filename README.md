# re1sid-lib

A small Python library for working with ReVanced CLI and patch bundles. It provides helpers to download required ReVanced assets and to patch APK files programmatically.

## Features

- Download the latest ReVanced CLI jar and patches.rvp bundle.
- List available patches and options using ReVanced CLI output parsing.
- Patch APK files with enabled/disabled patches and patch options.
- Preserve multi-line descriptions and compatibility metadata.

## Requirements

- Python 3.9+
- Java runtime installed and available in `PATH`
- Python packages:
  - `httpx`
  - `lxml`
  - `pyaxmlparser`

## Installation

```bash
pip install re1sid-lib
```

## Usage

### Download ReVanced assets

Use the downloader helper to fetch the latest CLI jar and patches bundle.

```python
from re1sid_lib import Downloader

downloader = Downloader()
downloader.download_all()
```

Or download individual assets:

```python
from re1sid_lib import Downloader

downloader = Downloader()
downloader.download_cli()
downloader.download_patches_rvp()
```

### List patches

Use the patcher helper to parse the patch list and inspect options.

```python
from re1sid_lib import Patcher

patcher = Patcher()
patches = patcher.list_patches(package_name="com.spotify.music")
for patch in patches:
    print(patch["Name"], patch["Enabled"])
```

### Patch an APK

Patch an APK file with specific enabled/disabled patches and options.

```python
from re1sid_lib import Patcher

patcher = Patcher()
output = patcher.patch_apk(
    apk_path="input.apk",
    output_path="patched.apk",
    enabled_patches=["remove-ads", 12],
    disabled_patches=["log-timestamp"],
    options={"theme": "dark", "ads": False},
    exclusive=False,
    force=True,
    bypass_verification=True,
    purge=True,
)
print(output)
```

## Notes

- `Downloader.download_all()` removes the `.revanced_res` directory before downloading fresh assets.
- `Patcher.list_patches()` can also accept an APK path and will read its package name automatically.
- The library expects the ReVanced CLI jar and patches bundle to exist at the configured paths.

## License

Use this project according to the appropriate license terms for ReVanced and its dependencies.