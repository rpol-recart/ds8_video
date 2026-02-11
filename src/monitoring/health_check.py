"""Health check script used by Docker HEALTHCHECK."""

import sys
import urllib.request


def main():
    try:
        resp = urllib.request.urlopen("http://localhost:8000/metrics", timeout=5)
        if resp.status == 200:
            sys.exit(0)
    except Exception:
        pass
    sys.exit(1)


if __name__ == "__main__":
    main()
