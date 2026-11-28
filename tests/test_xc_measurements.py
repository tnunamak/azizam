"""Expose the literal measurement in the hermetic JUnit artifact."""
import json

from tests.test_domain_literal_ratchet import all_sites, partition_reviewed_sites


def test_record_domain_literal_measurement(record_property):
    reviewed, unreviewed = partition_reviewed_sites(all_sites())
    record_property("domain_literals", str(sum(map(len, unreviewed.values()))))
    record_property("domain_literals_by_file", json.dumps({name: len(rows) for name, rows in unreviewed.items()}, sort_keys=True))
