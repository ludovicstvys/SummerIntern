import os

from test import detect_new_offers, read_process_csv, send_email, sync_new_offers_to_notion
from trackr_common import deduplicate_offers, filter_email_offers, log_run_summary, scrape_open_programmes, write_csv


SOURCE_URL = "https://app.the-trackr.com/hk-finance-2027-early-access-o/summer-internships"
TRACKR_PARAMS = {
    "region": "Hong Kong",
    "industry": "Finance",
    "season": "2027",
    "type": "summer-internships",
}
DEFAULT_OUTPUT_FILE = "processus_ouverts_hk_summer.csv"


def scrape_open_summer_internships():
    return scrape_open_programmes(TRACKR_PARAMS)


if __name__ == "__main__":
    output_file = os.getenv("OUTPUT_FILE", DEFAULT_OUTPUT_FILE)
    offers = deduplicate_offers(scrape_open_summer_internships())
    previous_offers = read_process_csv(output_file)
    force_email_all = os.getenv("FORCE_EMAIL_ALL", "").strip().lower() in ("1", "true", "yes")
    new_offers = detect_new_offers(offers, previous_offers)
    email_candidates = offers if force_email_all else new_offers
    log_run_summary(offers)
    notion_result = sync_new_offers_to_notion(new_offers, "offre(s) summer HK nouvelle(s)")
    csv_file = write_csv(offers, output_file)
    email_offers = filter_email_offers(email_candidates, notion_result)
    if email_offers:
        print(f"{len(email_offers)} nouvelle(s) offre(s) summer HK détectée(s), envoi email")
        send_email(email_offers, csv_file, "summer internship(s) HK")
    else:
        print("Aucune nouvelle offre summer HK détectée, email non envoyé")
