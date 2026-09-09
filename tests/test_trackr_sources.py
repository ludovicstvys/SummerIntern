from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from trackr_app.database import Base
from trackr_app.models import Offer, OfferSource
from trackr_app.scraper import TRACKERS, scrape_all
from trackr_common import OfferSnapshot, scrape_open_programmes


def test_spring_weeks_are_normalized_to_stable_page_urls():
    response = Mock()
    response.json.return_value = [{
        "id": "spring-1",
        "name": "Spring Insight Programme",
        "week": "Week 5",
        "dates": "13 April - 17 April",
        "companyId": "example",
        "companyName": "Example Capital",
    }]
    params = {
        "region": "UK",
        "source_type": "spring-weeks",
        "endpoint": "https://api.the-trackr.com/spring-weeks",
        "page_url": "https://app.the-trackr.com/uk-finance/spring-weeks",
    }
    with patch("trackr_common.requests.get", return_value=response) as request:
        offers = scrape_open_programmes(params)

    request.assert_called_once_with(
        "https://api.the-trackr.com/spring-weeks",
        params={"region": "UK"},
        timeout=30,
    )
    assert offers[0]["offer_url"] == "https://app.the-trackr.com/uk-finance/spring-weeks#spring-1"
    assert offers[0]["company"] == "Example Capital"
    assert offers[0]["stage"] == "Spring week"


def test_explicitly_closed_spring_weeks_are_not_returned_as_open():
    response = Mock()
    response.json.return_value = [
        {"id": "open", "name": "Open Spring Week", "companyName": "Open Co"},
        {"id": "closed", "name": "Closed Spring Week", "companyName": "Closed Co", "status": "closed"},
        {"id": "expired", "name": "Expired Spring Week", "companyName": "Expired Co", "isOpen": False},
    ]
    params = {
        "region": "UK",
        "source_type": "spring-weeks",
        "endpoint": "https://api.the-trackr.com/spring-weeks",
        "page_url": "https://app.the-trackr.com/uk-finance/spring-weeks",
    }
    with patch("trackr_common.requests.get", return_value=response):
        offers = scrape_open_programmes(params)

    assert [offer["name"] for offer in offers] == ["Open Spring Week"]


def test_programme_without_external_url_uses_stable_trackr_page_anchor():
    response = Mock()
    response.json.return_value = {"programmes": [{
        "id": "graduate-1", "name": "Graduate Analyst", "url": None,
        "openingDate": "2026-09-01T00:00:00.000Z", "company": {}, "categories": [],
    }]}
    params = {
        "region": "France", "industry": "Finance", "season": "2027",
        "type": "graduate-programmes", "page_url": "https://app.the-trackr.com/france-finance/graduate-programmes",
    }
    with patch("trackr_common.requests.get", return_value=response):
        offers = scrape_open_programmes(params)

    assert offers[0]["offer_url"] == "https://app.the-trackr.com/france-finance/graduate-programmes#graduate-1"


def test_requested_trackr_sources_are_registered():
    requested = {
        (source["region"], source["type"])
        for source in TRACKERS
        if source["type"] in {"spring-weeks", "industrial-placements", "graduate-programmes", "events"}
    }
    assert requested == {
        ("UK", "spring-weeks"),
        ("UK", "industrial-placements"),
        ("UK", "graduate-programmes"),
        ("UK", "events"),
        ("France", "graduate-programmes"),
    }


def test_spring_weeks_are_stored_as_a_source():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    item = {
        "name": "Spring Insight Programme",
        "offer_url": "https://app.the-trackr.com/uk-finance/spring-weeks#spring-1",
        "company": "Example Capital",
        "categories": ["Week 5"],
        "opening_date": None,
        "closing_date": None,
    }
    with patch("trackr_app.scraper.TRACKERS", [{
        "region": "UK",
        "type": "spring-weeks",
        "source_type": "spring-weeks",
        "page_url": "https://app.the-trackr.com/uk-finance/spring-weeks",
    }]), patch("trackr_app.scraper.scrape_open_programmes", return_value=[item]):
        result = scrape_all(db)

    offer = db.query(Offer).one()
    source = db.query(OfferSource).one()
    assert result["created"] == 1
    assert offer.programme_type == source.programme_type == "spring-weeks"
    assert source.region == "UK" and source.is_open


def test_missing_spring_week_is_closed_after_two_complete_snapshots():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    item = {
        "name": "Spring Insight Programme",
        "offer_url": "https://app.the-trackr.com/uk-finance/spring-weeks#spring-2",
        "company": "Example Capital",
        "categories": ["Week 5"],
        "opening_date": None,
        "closing_date": None,
    }
    source_config = {
        "region": "UK",
        "type": "spring-weeks",
        "source_type": "spring-weeks",
        "page_url": "https://app.the-trackr.com/uk-finance/spring-weeks",
    }
    with patch("trackr_app.scraper.TRACKERS", [source_config]), patch(
        "trackr_app.scraper.scrape_open_programmes",
        side_effect=[[item], OfferSnapshot(), OfferSnapshot()],
    ):
        scrape_all(db)
        offer = db.query(Offer).one()
        assert offer.is_open
        scrape_all(db)
        assert offer.is_open
        scrape_all(db)

    assert not offer.is_open
