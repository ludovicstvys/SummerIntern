import json
import unittest
from datetime import datetime, time, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from trackr_app.database import Base
from trackr_app.models import Delivery, Offer, Preference, User, UserOffer
from trackr_app.preferences import activate_preference, digest_is_due, infer_start_term, offer_matches, queue_new_offer


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.user = User(email="person@example.com")
        self.db.add(self.user)
        self.db.flush()
        self.preference = Preference(
            user_id=self.user.id,
            program_types=json.dumps(["off-cycle"]),
            regions=json.dumps(["France"]),
            start_terms=json.dumps(["2027 Q1 Start"]),
        )
        self.db.add(self.preference)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def make_offer(self, **changes):
        values = dict(canonical_url="https://example.com/job", offer_url="https://example.com/job", name="Analyst Intern", company="Example", region="France", programme_type="off-cycle", categories="[]", start_term="2027 Q1 Start")
        values.update(changes)
        offer = Offer(**values)
        self.db.add(offer)
        self.db.commit()
        return offer

    def test_start_term_is_inferred(self):
        self.assertEqual(infer_start_term(["Finance", "2027 Q3 Start"]), "2027 Q3 Start")

    def test_start_term_filter_only_affects_off_cycle(self):
        off_cycle = self.make_offer()
        self.assertTrue(offer_matches(off_cycle, self.preference))
        off_cycle.start_term = "2027 Q2 Start"
        self.assertFalse(offer_matches(off_cycle, self.preference))
        summer = Offer(canonical_url="https://example.com/summer", offer_url="https://example.com/summer", name="Summer", company="Example", region="France", programme_type="summer")
        self.preference.program_types = json.dumps(["summer"])
        self.assertTrue(offer_matches(summer, self.preference))

    def test_activation_creates_baseline_without_email(self):
        offer = self.make_offer()
        count = activate_preference(self.db, self.preference)
        self.assertEqual(count, 1)
        self.assertTrue(self.db.query(UserOffer).filter_by(user_id=self.user.id, offer_id=offer.id).one().baseline)
        self.assertEqual(self.db.query(Delivery).count(), 0)

    def test_new_matching_offer_is_idempotently_queued(self):
        self.preference.status = "active"
        offer = self.make_offer()
        queue_new_offer(self.db, offer)
        self.db.commit()
        queue_new_offer(self.db, offer)
        self.db.commit()
        self.assertEqual(self.db.query(UserOffer).count(), 1)
        self.assertEqual(self.db.query(Delivery).count(), 1)

    def test_digest_due_uses_user_timezone(self):
        self.preference.delivery_mode = "daily_digest"
        self.preference.digest_time = time(10, 0)
        self.preference.timezone = "Europe/Paris"
        winter_utc = datetime(2026, 1, 4, 9, 0, tzinfo=timezone.utc)
        self.assertTrue(digest_is_due(self.preference, winter_utc))


if __name__ == "__main__":
    unittest.main()
