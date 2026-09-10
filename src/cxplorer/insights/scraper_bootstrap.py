"""Load installed dependencies without running site startup hooks or inheriting PYTHONPATH."""

import json
import sys
from pathlib import Path


def main() -> None:
    sys.path.extend(json.loads(sys.argv[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from cxplorer.insights.scraper_worker import main as run

    run()


if __name__ == "__main__":
    main()
