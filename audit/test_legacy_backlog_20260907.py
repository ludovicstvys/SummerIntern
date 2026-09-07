"""Reproduction du mécanisme de l'incident, sans accès réseau ni base réelle.

Ce test constate le comportement actuel ; il ne valide pas le contrat métier.
"""
from unittest.mock import patch

from sqlalchemy.orm import sessionmaker

from tests.test_audit import db
from trackr_app.legacy import enqueue, run_collector
from trackr_app.models import LegacyTask
from trackr_common import write_csv


def test_old_pending_task_is_sent_even_when_csv_has_no_new_offer(db, tmp_path, monkeypatch):
    import test as adapter

    monkeypatch.setenv('LEGACY_NOTION_ENABLED', 'true')
    monkeypatch.setenv('LEGACY_EMAIL_ENABLED', 'false')
    item = dict(name='Already scraped', company='Example',
                offer_url='https://example.com/old', region='France',
                categories=[], opening_date='2026-09-01', closing_date=None,
                stage='Unknown', rolling=False, needs_cv=False,
                needs_cover_letter=False, company_id=None,
                company_description=None, notes=None)
    path = tmp_path / 'offers.csv'
    write_csv([item], path)
    assert adapter.detect_new_offers([item], adapter.read_process_csv(path)) == []
    enqueue(db, '2027/France/summer-internships', 'notion', '', item, 'Summer')
    db.commit()
    with (
        patch('trackr_app.legacy.SessionLocal', sessionmaker(bind=db.bind)),
        patch('trackr_app.legacy.scrape_open_programmes', return_value=[item]),
        patch.object(adapter, 'NOTION_TOKEN', 'test'),
        patch.object(adapter, 'NOTION_DATA_SOURCE_ID', 'test'),
        patch.object(adapter, 'prepare_notion_sync', return_value={
            'existing_offers': {}, 'data_source_id': 'test', 'schema': {}}),
        patch.object(adapter, 'sync_to_notion') as sync,
    ):
        assert run_collector({'season': '2027', 'region': 'France',
                              'type': 'summer-internships'}, path, 'Summer') == 0
    sync.assert_called_once()
    assert sync.call_args.args[0] == [item]
    assert db.query(LegacyTask).one().status == 'sent'
