import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from trackr_app.database import Base
from trackr_app.models import Offer
from trackr_app.scraper import scrape_all


def item(url: str):
    return {"offer_url": url, "name": "Intern", "company": "Example", "categories": []}


class ScraperIsolationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @patch("trackr_app.scraper.TRACKERS", [
        {"region": "France", "type": "summer-internships"},
        {"region": "Hong Kong", "type": "off-cycle-internships"},
    ])
    @patch("trackr_app.scraper.scrape_open_programmes")
    def test_one_failed_tracker_does_not_discard_successful_tracker(self, scrape):
        scrape.side_effect = [[item("https://example.com/one")], RuntimeError("temporary empty response")]
        result = scrape_all(self.db)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["failed_trackers"], 1)
        self.assertEqual(self.db.query(Offer).count(), 1)

    @patch("trackr_app.scraper.TRACKERS", [{"region": "France", "type": "summer-internships"}])
    @patch("trackr_app.scraper.scrape_open_programmes")
    def test_missing_offer_is_closed_only_after_valid_response(self, scrape):
        existing = Offer(canonical_url="https://example.com/old", offer_url="https://example.com/old", name="Old", region="France", programme_type="summer")
        self.db.add(existing)
        self.db.commit()
        scrape.return_value = [item("https://example.com/new")]
        result = scrape_all(self.db)
        self.assertEqual(result["closed"], 0)
        self.assertTrue(existing.is_open)
        result = scrape_all(self.db)
        self.assertEqual(result["closed"], 1)
        self.assertFalse(self.db.scalar(select(Offer).where(Offer.canonical_url == "https://example.com/old")).is_open)


if __name__ == "__main__":
    unittest.main()
