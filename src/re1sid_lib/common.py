import os

# Directory where downloaded ReVanced resources (CLI jar + patches bundle) live.
RESOURCE_DIR = os.path.join(os.getcwd(), ".revanced_res")

PATCHES_PATH = os.path.join(RESOURCE_DIR, "patches.rvp")
CLI_PATH = os.path.join(RESOURCE_DIR, "revanced-cli.jar")

# Where patched APKs get written by default.
OUTPUT_DIR = os.path.join(os.getcwd(), "output")