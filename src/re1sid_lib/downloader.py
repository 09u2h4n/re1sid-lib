import httpx
from typing import Generator, Optional, Union
import os
import shutil
from lxml import html

from common import PATCHES_PATH, CLI_PATH

class Downloader:
    def __init__(self) -> None:
        self.PATCHES_PATH = PATCHES_PATH
        self.CLI_PATH = CLI_PATH
        self.api_url = "https://api.revanced.app/v5/"

    def __download_common(self, url: str, save: bool = True, filename: Optional[str] = None,
    chunk_size: int = 8192) -> Union[bool, Generator[bytes, None, None]]:
        if filename is None:
            raise ValueError("Filename must be provided when save is True.")
        else:
            os.makedirs(os.path.dirname(filename), exist_ok=True)
        if save:
            with httpx.stream("GET", url, follow_redirects=True) as response:
                response.raise_for_status()
                with open(filename, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=chunk_size):
                        f.write(chunk)
            return True
        else:
            # Return an inner generator to keep the outer function's execution immediate when save=True
            def chunk_generator() -> Generator[bytes, None, None]:
                with httpx.stream("GET", url, follow_redirects=True) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(chunk_size=chunk_size):
                        yield chunk
                        
            return chunk_generator()
        
    def download_patches_rvp(self, save: bool = True, chunk_size: int = 8192) -> Union[bool, Generator[bytes, None, None]]:
        filename = self.PATCHES_PATH
        url = f"{self.api_url}patches.rvp"
        return self.__download_common(url, save=save, filename=filename, chunk_size=chunk_size)
    
    def download_cli(self, save: bool = True, chunk_size: int = 8192) -> Union[bool, Generator[bytes, None, None]]:
        filename = self.CLI_PATH
        api_url = "https://api.github.com/repos/ReVanced/revanced-cli/releases/latest"

        response = httpx.get(api_url, follow_redirects=True)

        if response.status_code == 403:
            repo_url = "https://github.com/ReVanced/revanced-cli/releases/latest"
            response = httpx.get(repo_url, follow_redirects=True)
            download_url = f"https://github.com/ReVanced/revanced-cli/releases/expanded_assets/{str(response.url).split("/")[-1]}"
            response = httpx.get(download_url, follow_redirects=True)
            html_content = html.fromstring(response.text)
            url = f"https://github.com{html_content.xpath('/html/body/div/ul/li[1]/div[1]/a/@href')[0]}"
            response.raise_for_status()
        else:
            response = response.json()
            assets = response.get("assets", [])
            url = None
            for asset in assets:
                name = asset.get("name", "")
                if name.endswith(".jar"):
                    url = asset.get("browser_download_url")
                    break
            if not url and assets:
                url = assets[0].get("browser_download_url")
            
            if not url:
                raise RuntimeError("Could not find a valid download URL in the ReVanced CLI release assets.")
            
        return self.__download_common(url, save=save, filename=filename, chunk_size=chunk_size)
    
    def download_all(self) -> None:
        shutil.rmtree(".revanced_res", ignore_errors=True)
        self.download_cli()
        self.download_patches_rvp()
    
if __name__ == "__main__":
    downloader = Downloader()
    downloader.download_all()
    