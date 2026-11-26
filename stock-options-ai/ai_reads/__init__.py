"""Weekly AI read of every name in the research universe.

One short, structured Claude read per company (five business dimensions, a
one-line thesis and a one-line risk), written to
forge-dashboard/data/robinhood/ai_reads.json. The dashboard ranks the reads
across the whole universe and feeds that rank into the research composite as
the `ai` factor, so the factor is mean-neutral and applies to every name
equally. Entry point: jobs/ai_universe_read.py. Policy: config/settings.py:AI_READS.

Why this exists: the previous `ai` factor came from a local model that read
35 of 598 names and said SELL on 77% of them, so in practice it was a
three-point penalty on whichever names it happened to read.
"""
