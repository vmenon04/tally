"""The Beancount ledger: tally's transactions written as Beancount entries.

Each row becomes a two-posting transaction dated by when it posted, since that is what
statements and running balances count; the day it happened is kept as `purchased` metadata
when it differs. Banks give no transaction IDs, so each entry carries `tally-id`, a hash of
the row's identity (the same fields tally.db deduplicates on), and descriptions are written
with long digit runs cut to their last four, so account and reference numbers stay out of
the ledger.
"""
