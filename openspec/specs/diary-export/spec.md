# Diary export behavior

- The exporter reads only the authenticated user's requested diary content. It does not create, edit or delete remote entries.
- The default workflow asks for credentials locally and does not persist or print passwords.
- Pagination follows the server's continuation signal, not an assumption that terminal pages are empty.
- Repeated page locations and pages that add no new record IDs must not produce an infinite loop or inflated totals.
- Progress reports unique records. Local checkpoints preserve successfully read data if a later request fails or the user interrupts.
- A complete account backup requires a verified record count and a natural end to enumeration. A browser-only history hint, failed request, explicit page cap or count mismatch is not success.
- Partial output is kept and accompanied by a machine-readable report and a nonzero exit status.
- JSON, TXT and Markdown preserve all extracted records; unknown dates do not silently discard entries.
- Text export and media download are separate scopes. Text-only results must not be advertised as including photographs or attachments.
- Personal exports, cookies, tokens and diagnostics are never published to the repository or CI.
