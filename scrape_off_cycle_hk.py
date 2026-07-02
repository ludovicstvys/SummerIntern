import os

from test import detect_new_offers, read_process_csv, send_email, sync_to_notion
from trackr_common import (
    deduplicate_offers,
    filter_email_offers,
    filter_offers_by_start_term,
    log_run_summary,
    scrape_open_programmes,
    write_csv,
)


SOURCE_URL = "https://app.the-trackr.com/hk-finance-2027-early-access-o/off-cycle-internships"
TRACKR_PARAMS = {
    "region": "Hong Kong",
    "industry": "Finance",
    "season": "2027",
    "type": "off-cycle-internships",
}
DEFAULT_OUTPUT_FILE = "processus_ouverts_hk_off_cycle.csv"
EMAIL_START_TERM = os.getenv("HK_OFF_CYCLE_EMAIL_START_TERM") or os.getenv("OFF_CYCLE_EMAIL_START_TERM", "2027 Q1 Start")


def scrape_open_off_cycle_internships():
    return scrape_open_programmes(TRACKR_PARAMS)


if __name__ == "__main__":
    output_file = os.getenv("OUTPUT_FILE", DEFAULT_OUTPUT_FILE)
    offers = deduplicate_offers(scrape_open_off_cycle_internships())
    previous_offers = read_process_csv(output_file)
    force_email_all = os.getenv("FORCE_EMAIL_ALL", "").strip().lower() in ("1", "true", "yes")
    new_offers = offers if force_email_all else detect_new_offers(offers, previous_offers)
    email_candidates = filter_offers_by_start_term(new_offers, EMAIL_START_TERM)
    log_run_summary(offers)
    csv_file = write_csv(offers, output_file)
    notion_offers = filter_offers_by_start_term(offers, EMAIL_START_TERM)
    print(f"{len(notion_offers)} offre(s) off-cycle HK {EMAIL_START_TERM} synchronisée(s) vers Notion")
    notion_result = sync_to_notion(notion_offers)
    email_offers = filter_email_offers(email_candidates, notion_result)
    if email_offers:
        print(
            f"{len(email_offers)} nouvelle(s) offre(s) off-cycle HK "
            f"{EMAIL_START_TERM} détectée(s), envoi email"
        )
        send_email(email_offers, csv_file, "off-cycle internship(s) HK")
    else:
        print(f"Aucune nouvelle offre off-cycle HK {EMAIL_START_TERM} détectée, email non envoyé")
