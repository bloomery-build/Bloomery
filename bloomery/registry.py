import json
import urllib.error
import urllib.request

from bloomery.errors import RegistryError

RAW_BASE = "https://raw.githubusercontent.com"
TIMEOUT = 10


class RegistryClient:
    def __init__(self, repo, branch="main"):
        self.repo = repo
        self.branch = branch

    def _url(self, filename):
        return f"{RAW_BASE}/{self.repo}/{self.branch}/{filename}"

    def _fetch(self, filename):
        url = self._url(filename)
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RegistryError(f"Not found in {self.repo}: {filename}") from e
            raise RegistryError(f"{self.repo} returned HTTP {e.code} for {filename}") from e
        except urllib.error.URLError as e:
            raise RegistryError(f"Could not reach {self.repo}: {e.reason}") from e

    def fetch_index(self):
        """The registry's index.json, parsed."""
        return json.loads(self._fetch("index.json"))

    def fetch_file(self, filename):
        """Raw bytes of one file in the registry."""
        return self._fetch(filename)
