# About TextDrop

TextDrop is a self-hosted text sharing service. Paste plain text or markdown, save it, and share the short link that comes back.

## How it works

- Submit text from the editor on the [home page](/).
- Each note is stored under a short generated ID and served at `/n/{id}`.
- The raw, unrendered text is always available at `/n/{id}/text`.
- Markdown notes are rendered to HTML, with the first heading used as the page title.

## Formatting

See the [formatting reference](/help/formatting) for the supported markdown syntax.
