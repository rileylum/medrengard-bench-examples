# Formatting Reference

TextDrop renders markdown notes with a CommonMark-style syntax, including tables, fenced code with syntax highlighting, and an automatic table of contents.

{{toc}}

## Headings

Start a line with one to six `#` characters:

```
# Heading 1
## Heading 2
### Heading 3
```

The first heading of a note becomes its page title.

## Emphasis

- `*italic*` or `_italic_` renders as *italic*
- `**bold**` or `__bold__` renders as **bold**
- `` `code` `` renders as `code`

## Links

Write `[text](https://example.com)` for a link, or wrap a URL in angle brackets: `<https://example.com>`.

## Lists

Unordered lists use `-`, `*` or `+`; ordered lists use numbers:

```
- apples
- oranges

1. first
2. second
```

## Block quotes

Prefix lines with `>`:

> Quoted text looks like this.

## Code blocks

Fence code with three backticks or tildes, optionally naming the language for syntax highlighting:

```python
def hello(name):
    return f"Hello, {name}!"
```

## Tables

```
| Name  | Value |
|-------|------:|
| alpha |     1 |
```

| Name  | Value |
|-------|------:|
| alpha |     1 |

## Horizontal rules

A line of three or more `-`, `*` or `_` characters draws a rule.

## Table of contents

Place `{{toc}}` on its own line to insert a table of contents built from the note's headings. Use `{{toc 2 3}}` to limit it to a range of heading levels.
