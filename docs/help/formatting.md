# Markdown Formatting Reference

TextDrop renders markdown notes with a built-in renderer that supports the common syntax shown below.

{{toc 2 3}}

## Headings

Start a line with one to six `#` characters:

```
# Heading 1
## Heading 2
### Heading 3
```

The first heading in a note becomes its page title.

## Emphasis

- `*italic*` or `_italic_` renders as *italic*
- `**bold**` or `__bold__` renders as **bold**
- `~~strikethrough~~` renders as ~~strikethrough~~
- `` `code` `` renders as `code`

## Links and images

- `[link text](https://example.com)` creates a link
- `![alt text](https://example.com/image.png)` embeds an image
- `<https://example.com>` creates an automatic link

## Lists

```
- unordered item
- another item

1. ordered item
2. another item
```

## Block quotes

```
> Quoted text
```

## Code blocks

Wrap code in triple backticks, optionally naming the language for syntax highlighting:

````
```python
print("hello")
```
````

## Tables

```
| Column A | Column B |
|:---------|---------:|
| left     |    right |
```

## Horizontal rules

A line containing `---`, `***` or `___` produces a horizontal rule.

## Table of contents

A line containing only `{{toc}}` inserts a table of contents built from the headings. Use `{{toc 2 3}}` to limit it to heading levels 2 through 3.
