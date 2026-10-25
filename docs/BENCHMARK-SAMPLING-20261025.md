# DeepSeek Benchmark sampling contract

## Evidence and units

- Results retain schema=2; memory_schema=1 uses named integer byte fields.
- GB = bytes / 1e9; GiB = bytes / 2^30.
- Working Set (psutil Windows rss), process Private Commit and Windows system
  Commit Total/Limit are separate metrics. VMS is not a Private Commit substitute.
- CPU expert copy totals come from completed engine loading log lines. They do not
  establish current physical residency, Working Set or peak request memory.
- sampled_at is an instantaneous, non-atomic observation, not a request average or peak.
- Missing, denied, ambiguous or unsupported measurements remain null/Unknown, never zero.

## Process identity

Require structured launch evidence matching executable path, model pack, full argv
and a unique process created within ten seconds of the recorded start. Recheck
creation time, executable, argv and liveness before and after sampling to reject
PID reuse. No name-only process selection is allowed.

Process sampling requires psutil in the Python environment running the TUI.
After verified identity only, AccessDenied may fall back to exact PID/name tasklist
CSV parsing. Its K values are KiB; it cannot supply Private Commit.

## History and configuration

Snapshots are append-only JSONL under logs/deepseek-bench.jsonl. Tests use isolated
files. Legacy ram_gb is untrusted because of historical CSV/PID/unit ambiguity;
it is not automatically migrated or converted into a residency measurement.

Configured argv is distinct from Effective startup-log evidence. Omitted arguments
are Auto, not proof of a fixed Default. Defaults need version-specific evidence;
otherwise display Unknown. GPU expert counts must never be used as the
--vram-experts ratio. Details retain raw saved records and available provenance.

History markers show latest, best Decode and best Prefill, but these are not
controlled A/B comparisons across different prompts, models or reuse states.

## Official DSV41 0.1.51

Official builds report completed CPU-copy totals and startup prefill chunk values.
The experimental --expert-copy-gb option is not supported by the official CLI and
is not offered in the current settings UI. Old experimental records remain readable;
restoring their unsupported manual budget must fail validation rather than silently
passing it to the official engine.

## Validation

Run python run_tests.py with a configured official source checkout and dependencies.
Fixtures exercise CSV parsing, process identity, denied/missing samples, PID reuse,
legacy records and UI navigation without starting a model or downloading weights.
