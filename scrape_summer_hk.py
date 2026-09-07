"""Compatibility entry point; validation and queues are shared."""
import os
from trackr_app.config import settings
from trackr_app.legacy import run_collector
from trackr_common import scrape_open_programmes, deduplicate_offers, write_csv

TRACKR_PARAMS = {'region': 'Hong Kong', 'industry': 'Finance', 'season': settings.season, 'type': 'summer-internships'}
DEFAULT_OUTPUT_FILE = 'processus_ouverts_hk_summer.csv'


def scrape_open_summer_internships():
    return scrape_open_programmes(TRACKR_PARAMS)


if __name__ == '__main__':
    raise SystemExit(run_collector(TRACKR_PARAMS, os.getenv('OUTPUT_FILE', DEFAULT_OUTPUT_FILE), 'Hong Kong summer-internships', None))
