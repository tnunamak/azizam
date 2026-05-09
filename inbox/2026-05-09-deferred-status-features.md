# Deferred status-page features (2026-05-09)

These were in the original spec for the status-page polish pass but pushed
out for clear reasons — captured here so the next pass can pick them up
without re-scoping.

- **Storage trend (sparkline of disk used over time)** — needs persistence.
  We currently only sample disk on each request, so we can't draw a trend
  without a sidecar logger. Smallest path: a tiny TSV under
  `cache/disk-trend.tsv`, appended every N minutes by a systemd timer.
- **5-min thermal max** — needs a sampler. Single-shot temps are noisy and
  miss spikes. Want a rolling 5-min max per-zone, which means either a
  background task in the proxy or scraping `journalctl -k` for thermal
  throttle events.
- **Network throughput (per-NIC TX/RX rate)** — too noisy to be useful as a
  single number. Needs a 1-min smoothed rate plus per-service attribution
  via cgroups (which we don't expose yet). Can revisit when the RAM
  attribution pattern proves itself.
