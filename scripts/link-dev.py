#!/usr/bin/env python3
"""Generate an ignored development manifest and link this checkout to Herdr."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true",
                        help="generate the manifest without registering it")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    source = (root / "herdr-plugin.toml").read_text()
    official = tomllib.loads(source)
    namespace, separator, slug = official["id"].rpartition(".")
    if not separator or not namespace or not slug:
        parser.error("the official plugin ID must include a namespace")
    name = "dev-" + slug
    plugin_id = namespace + "." + name
    generated = source
    for field, value in (("id", plugin_id), ("name", name)):
        generated, count = re.subn(
            rf"(?m)^{field}\s*=\s*[^\n]*$",
            lambda match: field + " = " + json.dumps(value),
            generated, count=1,
        )
        if count != 1:
            parser.error("missing top-level " + field)
    # Use the development label in action and pane titles too. Commands stay
    # relative to this checkout, so edits do not need to be copied elsewhere.
    generated = generated.replace(
        'title = "' + official["name"], 'title = "' + name,
    )
    development = tomllib.loads(generated)
    assert development["id"] == plugin_id and development["name"] == name
    manifest = root / "herdr-plugin.dev.toml"
    manifest.write_text(generated)
    print(f"{name} [{plugin_id}]: {manifest}", flush=True)
    if args.prepare_only:
        return 0
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    return subprocess.run(
        [herdr, "plugin", "link", str(manifest), "--enabled"], check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
