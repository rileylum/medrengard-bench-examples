# About TextDrop

TextDrop is a small, self-hosted service for sharing plain text and markdown notes through short, easy-to-share URLs.

## How it works

1. Paste your text into the editor on the [home page](/).
2. Choose **Plain text** or **Markdown** as the format.
3. Submit, and TextDrop stores your note under a short generated ID.

Every note is available in two forms:

- a rendered view at `/n/{id}`
- the raw text at `/n/{id}/text`

## Privacy

Notes are kept in memory only and disappear when the service restarts. Note pages are excluded from search engine crawling via `/robots.txt`.

## Learn more

See the [formatting reference](/help/formatting) for the markdown syntax TextDrop supports.
