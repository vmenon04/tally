# Importing bank exports

Download the CSVs (Discover: Recent Activity or a statement; BofA checking: any date range),
put them in `data/inbox/`, then:

```sh
uv run tally import --dry-run   # show what would be added, save nothing
uv run tally import             # store new rows, archive each file to data/raw/, empty the inbox
```

The bank is recognized from the file's contents, so file names don't matter. Overlapping
downloads are fine: rows already stored are skipped. A file that fails (unknown layout, running
balances that don't add up, a gap in BofA history) is not stored and stays in the inbox.
`uv run tally import FILE...` imports specific files and leaves them where they are.

`TALLY_DATA_DIR` sets the data folder (default: `data` in the current directory).
