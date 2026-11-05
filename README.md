# babel_code_goat

```
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python babel_code_goat.py generate <tests_dir> --entrypoint <name> --lang python|javascript|typescript
python babel_code_goat.py test <solution> <tests_dir> --lang python|javascript|typescript [--timeout SECONDS]
```

JavaScript/TypeScript testers require Node.js (TypeScript uses Node's built-in type stripping, Node >= 22.6).
