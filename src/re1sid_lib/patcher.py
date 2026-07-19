import subprocess
import os
from typing import List, Dict, Any, Union, Optional, Generator
from pyaxmlparser import APK

from .common import PATCHES_PATH, CLI_PATH


class Patcher:
    def __init__(self) -> None:
        self.PATCHES_PATH = PATCHES_PATH
        self.CLI_PATH = CLI_PATH

    def __exec_cmd(
        self, cmd: List[str], stream: bool = False
    ) -> Union[str, Generator[str, None, None]]:
        """
        Executes a command.

        :param cmd: The command to run as a list of strings.
        :param stream: If True, returns a generator that yields output line-by-line.
                    If False, blocks and returns the entire stdout as a string.
        """
        if stream:

            def line_generator() -> Generator[str, None, None]:
                process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                )

                if process.stdout:
                    for line in iter(process.stdout.readline, ""):
                        yield line.strip()

                return_code = process.wait()
                if return_code != 0:
                    raise RuntimeError(f"Command failed with exit code {return_code}.")

            return line_generator()

        else:
            # Standard blocking execution
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            stdout, stderr = process.communicate()
            stdout_str = stdout.strip()
            stderr_str = stderr.strip()

            if process.returncode != 0:
                err_msg = stderr_str if stderr_str else stdout_str
                raise RuntimeError(
                    f"Command failed with exit code {process.returncode}. Error: {err_msg}"
                )
            return stdout_str

    def __get_package_infos(self, package_name: Optional[str] = None) -> str:
        if not os.path.exists(self.PATCHES_PATH) or not os.path.exists(self.CLI_PATH):
            raise FileNotFoundError(
                "Required files not found. Please ensure both patches and CLI are available."
            )
        cmd = [
            "java",
            "-jar",
            self.CLI_PATH,
            "list-patches",
            "--packages",
            "--versions",
            "--options",
            "-b",
            "-p",
            self.PATCHES_PATH,
        ]
        if package_name:
            cmd.append(f"--filter-package-name={package_name}")
        return self.__exec_cmd(cmd)

    def __parse_package_infos(
        self, package_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Parses the CLI output of the ReVanced list-patches command into
        a structured list of dictionaries.
        """
        text = self.__get_package_infos(package_name)
        patches = []
        current_patch = None
        current_option = None
        current_package = None

        # Track parser state for multi-line values/descriptions
        state = None  # Can be: None, "patch_desc", "opt_desc", "values", "versions"

        for line in text.splitlines():
            # Strip common logging prefixes if present
            if line.startswith("INFO: "):
                line = line[6:]

            stripped = line.strip()

            # Handle empty lines
            if not stripped:
                # Preserve empty lines inside multi-line descriptions
                if state == "patch_desc" and current_patch:
                    current_patch["Description"] += "\n"
                elif state == "opt_desc" and current_option:
                    current_option["Description"] += "\n"
                continue

            # Determine indentation level (supporting both tabs and spaces)
            tabs = len(line) - len(line.lstrip("\t"))
            if tabs == 0:
                spaces = len(line) - len(line.lstrip(" "))
                tabs = spaces // 4  # Standard conversion of 4 spaces to 1 tab level

            # --- LEVEL 0: Main Patch Parameters ---
            if tabs == 0:
                if stripped.startswith("Index:"):
                    # Save previous patch before starting a new one
                    if current_patch:
                        patches.append(current_patch)

                    index_val = int(stripped.split(":", 1)[1].strip())
                    current_patch = {
                        "Index": index_val,
                        "Name": "",
                        "Description": "",
                        "Enabled": False,
                        "Options": [],
                        "Compatible packages": [],
                    }
                    current_option = None
                    current_package = None
                    state = None

                elif current_patch:
                    if stripped.startswith("Name:"):
                        current_patch["Name"] = stripped.split(":", 1)[1].strip()
                        state = None
                    elif stripped.startswith("Description:"):
                        current_patch["Description"] = stripped.split(":", 1)[1].strip()
                        state = "patch_desc"
                    elif stripped.startswith("Enabled:"):
                        current_patch["Enabled"] = (
                            stripped.split(":", 1)[1].strip().lower() == "true"
                        )
                        state = None
                    elif stripped in ("Options:", "Compatible packages:"):
                        state = None

            # --- LEVEL 1: Options & Compatible Packages ---
            elif tabs == 1:
                if current_patch:
                    if stripped.startswith("Name:"):
                        current_option = {
                            "Name": stripped.split(":", 1)[1].strip(),
                            "Description": "",
                            "Required": False,
                            "Default": None,
                            "Possible values": [],
                            "Type": "",
                        }
                        current_patch["Options"].append(current_option)
                        current_package = None
                        state = None

                    elif stripped.startswith("Package name:"):
                        current_package = {
                            "Package name": stripped.split(":", 1)[1].strip(),
                            "Compatible versions": [],
                        }
                        current_patch["Compatible packages"].append(current_package)
                        current_option = None
                        state = None

                    elif current_option:
                        if stripped.startswith("Description:"):
                            current_option["Description"] = stripped.split(":", 1)[
                                1
                            ].strip()
                            state = "opt_desc"
                        elif stripped.startswith("Required:"):
                            current_option["Required"] = (
                                stripped.split(":", 1)[1].strip().lower() == "true"
                            )
                            state = None
                        elif stripped.startswith("Default:"):
                            val = stripped.split(":", 1)[1].strip()
                            if val.lower() == "true":
                                current_option["Default"] = True
                            elif val.lower() == "false":
                                current_option["Default"] = False
                            else:
                                current_option["Default"] = val
                            state = None
                        elif stripped.startswith("Type:"):
                            current_option["Type"] = stripped.split(":", 1)[1].strip()
                            state = None
                        elif stripped.startswith("Possible values:"):
                            state = "values"
                        else:
                            # Append any un-keyed text here to support multi-line descriptions
                            if state == "opt_desc":
                                current_option["Description"] += "\n" + stripped

                    elif current_package:
                        if stripped.startswith("Compatible versions:"):
                            state = "versions"

            # --- LEVEL 2: Nested Lists ---
            elif tabs == 2:
                if state == "values" and current_option:
                    current_option["Possible values"].append(stripped)
                elif state == "versions" and current_package:
                    current_package["Compatible versions"].append(stripped)
                elif state == "opt_desc" and current_option:
                    current_option["Description"] += "\n" + stripped

        # Append the final patch
        if current_patch:
            patches.append(current_patch)

        # Clean up excess whitespace from multi-line descriptions
        for patch in patches:
            patch["Description"] = patch["Description"].strip()
            for opt in patch["Options"]:
                opt["Description"] = opt["Description"].strip()

        return patches

    def list_patches(
        self, package_name: Optional[str] = None, apk_path: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Public method to retrieve and parse the list of patches, optionally filtered by a package.
        """
        if apk_path:
            package_info = self.get_apk_info(apk_path)["package_name"]
            return self.__parse_package_infos(package_info)
        return self.__parse_package_infos(package_name)

    def get_apk_info(self, apk_path: str) -> Dict[str, str]:
        """
        Reads metadata from the APK file and returns its package name and version.
        """
        if not os.path.exists(apk_path):
            raise FileNotFoundError(f"APK file not found at path: {apk_path}")
        apk = APK(apk_path)
        return {"package_name": apk.package, "version_name": apk.version_name}

    def _format_option_value(self, val: Any) -> str:
        if val is True:
            return "true"
        elif val is False:
            return "false"
        elif isinstance(val, list):
            return f"[{','.join(self._format_option_value(item) for item in val)}]"
        elif val is None:
            return ""
        else:
            return str(val)

    def patch_apk(
        self,
        apk_path: str,
        output_path: Optional[str] = None,
        enabled_patches: Optional[List[Union[str, int]]] = None,
        disabled_patches: Optional[List[Union[str, int]]] = None,
        options: Optional[Dict[str, Any]] = None,
        exclusive: bool = False,
        force: bool = False,
        bypass_verification: bool = True,
        purge: bool = True,
        stream_output: bool = False,
    ) -> Union[str, Generator[str, None, None]]:
        """
        Patches an APK file using the ReVanced CLI.

        :param apk_path: Path to the input APK file.
        :param output_path: Path to save the patched APK.
        :param enabled_patches: Names or indices of patches to enable.
        :param disabled_patches: Names or indices of patches to disable.
        :param options: Dict of option values keyed by option keys.
        :param exclusive: If True, only specified enabled patches will be applied.
        :param force: If True, compatibility checks will be bypassed.
        :param bypass_verification: If True, bypass signature/provenance check on RVP files.
        :param purge: If True, purge temporary files directory after patching.
        :param stream_output: If True, returns a generator that yields output line-by-line.
        :return: Standard output of the patch command.
        """
        if not os.path.exists(self.PATCHES_PATH) or not os.path.exists(self.CLI_PATH):
            raise FileNotFoundError(
                "Required files not found. Please ensure both patches and CLI are available."
            )
        if not os.path.exists(apk_path):
            raise FileNotFoundError(f"APK file not found at path: {apk_path}")

        cmd = ["java", "-jar", self.CLI_PATH, "patch", "-p", self.PATCHES_PATH]
        if bypass_verification:
            cmd.append("-b")

        if exclusive:
            cmd.append("--exclusive")
        if force:
            cmd.append("-f")
        if output_path:
            cmd.extend(["-o", output_path])

        if enabled_patches:
            for patch in enabled_patches:
                if isinstance(patch, int):
                    cmd.extend(["--ei", str(patch)])
                else:
                    cmd.extend(["-e", str(patch)])

        if disabled_patches:
            for patch in disabled_patches:
                if isinstance(patch, int):
                    cmd.extend(["--di", str(patch)])
                else:
                    cmd.extend(["-d", str(patch)])

        if options:
            for key, val in options.items():
                if val is None:
                    cmd.append(f"-O{key}")
                else:
                    cmd.append(f"-O{key}={self._format_option_value(val)}")

        if purge:
            cmd.append("--purge")

        cmd.append(apk_path)
        print(" ".join(cmd))
        return self.__exec_cmd(cmd, stream=stream_output)
    

if __name__ == "__main__":
    patcher = Patcher()
    # Example usage: List patches for a specific package
    try:
        patches = patcher.list_patches(package_name="com.spotify.music")
        for patch in patches:
            print(patch)
    except Exception as e:
        print(f"Error: {e}")